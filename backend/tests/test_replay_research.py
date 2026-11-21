import os
from datetime import date, datetime, time, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from quantlab import db, llm, pod_store, research
from quantlab.config import DB_DIR


def test_replay_model_keeps_news_but_withholds_absolute_clock():
    brief = {
        "as_of": "2025-04-15T14:30:00+08:00",
        "market_day": "2025-04-14",
        "news": {"N2": {"title": "银行净利润回升", "excerpt": "贷款利率下降。"}},
        "candidates": [
            {"board": "银行", "intraday": {"at": "2025-04-15T14:25:00+08:00", "return": 0.01}}
        ],
        "paper_account": {
            "holdings": [],
            "recent_orders": [
                {"code": "999", "side": "buy", "amount": 10, "execute_on": "2025-04-11"}
            ],
        },
    }
    prompt = llm._relative_replay_brief(brief)
    assert "2025-04" not in str(prompt) and prompt["last_close_age_days"] == 1
    assert (
        prompt["news"] == brief["news"]
        and prompt["paper_account"]["recent_orders"][0]["days_ago"] == 4
    )


def test_online_database_cannot_be_used_for_replay(monkeypatch):
    monkeypatch.setattr(db, "query_one", lambda *a: {"name": "quantlab"})
    with pytest.raises(RuntimeError, match="独立"):
        research._assert_replay_database()
    monkeypatch.setattr(db, "query_one", lambda *a: {"name": "quantlab_replay_unit"})
    monkeypatch.setattr(db, "query", lambda *a: [{"mode": "live"}])
    with pytest.raises(RuntimeError, match="historical_replay"):
        research._assert_replay_database()


def test_legacy_strategy_cannot_reopen_retired_accounts_in_empty_live_db(monkeypatch):
    monkeypatch.setattr(db, "query_one", lambda *a: {"name": "quantlab"})
    with pytest.raises(RuntimeError, match="独立"):
        pod_store.create_pod(
            name="板块集中轮动", strategy_key="momentum_flow", thesis="旧策略",
            invalidation_rules=[], entry_evidence={}, trade_date=date(2026, 11, 20),
        )

    rows = iter(({"name": "quantlab_replay_unit"}, {"found": 1}))
    monkeypatch.setattr(db, "query_one", lambda *a: next(rows))
    with pytest.raises(RuntimeError, match="基金经理回放"):
        pod_store.assert_legacy_replay_database()


@pytest.fixture
def history_schema(monkeypatch):
    if os.getenv("QUANTLAB_TEST_POSTGRES") != "1":
        pytest.skip("set QUANTLAB_TEST_POSTGRES=1 for the isolated historical replay fixture")
    schema = "replay_test_" + uuid4().hex
    monkeypatch.setattr(db, "_schema", schema)
    with db.transaction() as conn:
        for f in ("schema.sql", "agent_research.sql"):
            conn.execute((DB_DIR / f).read_text(encoding="utf8").replace("quantlab", schema))
    try:
        # The fixture uses a temporary schema; the production database-name check has its own test.
        monkeypatch.setattr(research, "_assert_replay_database", lambda: None)
        yield schema
    finally:
        db.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.parametrize("strategy_version", ["v1", "v2"])
def test_backfilled_news_prices_and_forecasts_obey_simulated_clock(
    history_schema, strategy_version
):
    s = history_schema
    days = list(pd.bdate_range("2025-04-01", periods=100).date)
    day = days[91]
    at = datetime.combine(day, time(14, 30), research.SH)
    db.execute_many(f"INSERT INTO {s}.trade_calendar(trade_date) VALUES(%s)", [(d,) for d in days])
    pod_store.create_pod(
        name="隔离历史经理",
        strategy_key="agent_manager",
        thesis="验证",
        invalidation_rules=[],
        entry_evidence={"mode": "historical_replay", "strategy_version": strategy_version},
        trade_date=day,
    )
    db.execute(
        f"INSERT INTO {s}.board(board_id,code,name,kind) VALUES (1,'IDX','保险','industry'),(2,'PROXY','银行','industry')"
    )
    db.execute(
        f"INSERT INTO {s}.instrument(instrument_id,code,name,kind) VALUES(1,'111111','保险基金','otc'),(2,'222222','银行基金','otc')"
    )
    db.execute(f"INSERT INTO {s}.instrument_board(instrument_id,board_id) VALUES(1,1),(2,2)")
    price = lambda i: 1.005**i if i < 91 else 99 + i
    db.execute_many(
        f"INSERT INTO {s}.instrument_nav(instrument_id,trade_date,nav) VALUES(%s,%s,%s)",
        [(asset, d, price(i)) for i, d in enumerate(days) for asset in (1, 2)],
    )
    db.execute_many(
        f"INSERT INTO {s}.board_quote(board_id,trade_date,close) VALUES(1,%s,%s)",
        [(d, price(i)) for i, d in enumerate(days)],
    )
    db.execute_many(
        f"INSERT INTO {s}.news_item(source,published_at,title,content_hash) VALUES('chinanews-finance',%s,%s,%s)",
        [
            (at - timedelta(minutes=1), "银行需求", "visible"),
            (at, "同一分钟发布时间不明", "minute-boundary"),
            (at + timedelta(minutes=1), "保险重大意外", "late"),
        ],
    )
    report = research.generate_replay_research(
        at, use_model=False, strategy_version=strategy_version
    )
    if strategy_version == "v2":
        assert report["evidence"]["calibration"] == {}
        assert report["watchlist"][0]["outlooks"][0]["raw_prob_up"] > 0.5
    assert (
        report["mode"] == "rules"
        and report["targets"]
        and report["run_mode"] == "historical_replay"
    )
    assert report["data_as_of"] == str(days[90]) and [
        n["title"] for n in report["evidence"]["news"]
    ] == ["银行需求"]
    assert all(abs(r["momentum_5"] - (1.005**5 - 1)) < 0.0001 for r in report["watchlist"])
    assert len(report["forecasts"]) == 4 and report["memories"] == []
    maturity = datetime.combine(days[92], time(14, 30), research.SH)
    assert research.settle_replay_forecasts(maturity) == 0
    assert research.settle_replay_forecasts(maturity.replace(hour=16)) == 1
    assert (
        research.settle_replay_forecasts((maturity + timedelta(days=1)).replace(hour=8, minute=25))
        == 0
    )
    assert (
        research.settle_replay_forecasts((maturity + timedelta(days=1)).replace(hour=8, minute=30))
        == 1
    )
    replayed = research.generate_replay_research(
        at, use_model=False, strategy_version=strategy_version
    )
    assert replayed["id"] == report["id"] and all(
        f["status"] == "pending" for f in replayed["forecasts"]
    )
    assert not research._memory(at)
    assert {m["board"] for m in research._memory(maturity + timedelta(days=1))} == {"保险", "银行"}
