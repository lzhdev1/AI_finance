"""The HTTP queue survives browser reloads and cannot execute a paper trade."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from uuid import uuid4

import pytest
import pandas as pd

from quantlab import db, station
from quantlab.config import DB_DIR


@pytest.fixture
def desk(monkeypatch):
    if os.getenv("QUANTLAB_TEST_POSTGRES") != "1":
        pytest.skip("set QUANTLAB_TEST_POSTGRES=1 for PostgreSQL integration tests")
    schema = "desk_test_" + uuid4().hex
    monkeypatch.setattr(db, "_schema", schema)
    try:
        with db.transaction() as conn:
            for filename in ("schema.sql", "agent_research.sql"):
                conn.execute(
                    (DB_DIR / filename).read_text(encoding="utf-8").replace("quantlab", schema)
                )
        yield
    finally:
        db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_manual_queue_restart_and_dashboard(desk, monkeypatch):
    def enqueue(_):
        try:
            return station.queue_research()["run_id"]
        except station.ResearchBusy:
            return None

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(enqueue, [1, 2]))
    assert sum(i is not None for i in ids) == 1
    # Simulate a process crash after claiming the job, before the report was committed.
    db.execute(
        f"""UPDATE {db.qualified("pipeline_run")} SET stats='{{"stage":"analysis"}}'::jsonb"""
    )
    station.process_queue()
    assert station.history("runs")["items"][0]["status"] == "failed"

    monkeypatch.setattr(station.ingest, "update_news", lambda **kwargs: 0)
    station.ensure_manager()
    station.heartbeat()
    station.queue_research()
    station.process_queue()
    state = station.dashboard()
    assert state["status"]["scheduler_online"]
    assert state["report"]["mode"] == "insufficient"
    assert state["report"]["targets"] is None
    assert state["manager"]["cash"] == state["manager"]["initial_capital"]
    assert state["manager"]["holdings"] == state["orders"] == state["decisions"] == []
    assert station.history("reports")["items"][0]["id"] == state["report"]["id"]
    assert station.history("runs", limit=1)["has_more"]


def test_public_workbench_hides_raw_pipeline_errors(desk):
    runs = db.qualified("pipeline_run")
    db.execute(f"""INSERT INTO {runs}(run_date,phase,status,stats,error) VALUES
        (current_date,'fund_nav','failed','{{"stage":"collect","warnings":["private diagnostic"]}}',
         'connection details')""")
    assert station.history("runs")["items"][0]["error"] == "connection details"
    assert station.history("events")["items"][0]["detail"] == "connection details"
    desk_view = station.dashboard(public=True)
    assert desk_view["runs"][0]["stats"] == {"stage": "collect"}
    assert "error" not in desk_view["runs"][0]
    assert desk_view["events"][0]["detail"] == "collect"


def test_behavior_is_frozen_only_when_a_board_forecast_is_first_issued(desk):
    from quantlab import research

    t = db.qualified
    bid = db.query_one(f"""INSERT INTO {t("board")}(code,name,kind) VALUES
        ('bank','银行','industry') RETURNING board_id""")["board_id"]
    report = {"as_of": "2026-11-20T14:30:00+08:00", "data_as_of": "2026-11-19", "mode": "rules"}
    old = {
        "board_id": bid,
        "board": "银行",
        "proxy_code": "",
        "base_date": "2026-11-20",
        "target_date": "2026-11-23",
        "horizon_days": 1,
        "prob_up": 0.51,
        "expected_return": 0.001,
        "confidence": "low",
        "samples": 15,
    }
    new = dict(
        old,
        target_date="2026-11-27",
        horizon_days=5,
        behavior_version=1,
        behavior_regime="demand",
        base_prob_up=0.52,
        behavior_feedback={"samples": 0},
    )
    research._persist(report, [old, new])
    attempted = dict(new, behavior_regime="distribution", prob_up=0.4)
    second = research._persist(
        dict(report, as_of="2026-11-20T17:00:00+08:00"),
        [attempted, dict(attempted, horizon_days=1, target_date="2026-11-23")],
    )
    observations = db.query(f"""SELECT f.horizon_days,f.prob_up,o.regime FROM {t("agent_board_forecast")} f
                    LEFT JOIN {t("agent_behavior_observation")} o USING(forecast_id) ORDER BY f.horizon_days""")
    assert observations == [
        {"horizon_days": 1, "prob_up": 0.51, "regime": None},
        {"horizon_days": 5, "prob_up": 0.51, "regime": "demand"},
    ]
    assert len(research._forecasts(second)) == 2


def test_weekend_research_charges_for_the_next_possible_subscription(desk, monkeypatch):
    from quantlab import research

    class Saturday(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff.astimezone(tz)

    cutoff = Saturday(2026, 11, 21, 14, 30, tzinfo=research.SH)
    monkeypatch.setattr(research, "datetime", Saturday)
    monkeypatch.setattr(station.llm, "research_queries", lambda *args: ([], "测试"))
    monkeypatch.setattr(station.llm, "research_opinion", lambda context: (None, "测试", None))
    monkeypatch.setattr(
        research.pod_store,
        "fee_book",
        lambda codes: {"014028": {"buy": 0, "sell": [[7, 0.015], [999, 0]]}},
    )
    t = db.qualified
    station.ensure_manager()
    sessions = list(pd.bdate_range(end="2026-12-10", periods=160).date)
    known = [d for d in sessions if d < cutoff.date()]
    db.execute_many(
        f"INSERT INTO {t('trade_calendar')}(trade_date) VALUES (%s)", [(d,) for d in sessions]
    )
    bid = db.query_one(
        f"INSERT INTO {t('board')}(code,name,kind) VALUES('bank','银行','industry') RETURNING board_id"
    )["board_id"]
    fid = db.query_one(
        f"INSERT INTO {t('instrument')}(code,name,kind) VALUES('014028','银行指数C','otc') RETURNING instrument_id"
    )["instrument_id"]
    db.execute(
        f"INSERT INTO {t('instrument_board')}(board_id,instrument_id) VALUES(%s,%s)", [bid, fid]
    )
    rows = [(d, 1 + i * 0.0005, cutoff - timedelta(hours=16)) for i, d in enumerate(known)]
    db.execute_many(
        f"INSERT INTO {t('board_quote')}(board_id,trade_date,close,ingested_at) VALUES(%s,%s,%s,%s)",
        [(bid, *r) for r in rows],
    )
    db.execute_many(
        f"INSERT INTO {t('instrument_nav')}(instrument_id,trade_date,nav,ingested_at) VALUES(%s,%s,%s,%s)",
        [(fid, *r) for r in rows],
    )
    report = research.generate_research(cutoff)
    fees = report["evidence"]["fund_features"]["014028"]
    assert (fees["application_date"], fees["evaluation_date"], fees["holding_days"]) == (
        "2026-11-23",
        "2026-11-27",
        4,
    )
    assert fees["round_trip_fee"] == pytest.approx(1 / 0.985 - 1)
    assert report["watchlist"][0]["intent"] == "allocate"
    assert report["targets"] == {} and "赎回费率 1.50%" in report["rationale"]
    assert len(report["fund_forecasts"]) == 2 and all(f["id"] for f in report["fund_forecasts"])


def test_live_trading_clock_excludes_overnight_and_late_instructions(monkeypatch):
    from quantlab import research, runner

    for hour, minute, blocked in (
        (0, 1, True),
        (14, 29, True),
        (14, 30, False),
        (14, 48, False),
        (15, 0, True),
    ):
        stamp = datetime(2026, 11, 20, hour, minute, tzinfo=research.SH)
        monkeypatch.setattr(station, "now", lambda stamp=stamp: stamp)
        assert runner._missed_cutoff(stamp.date()) is blocked


def test_research_to_paper_fill_to_forecast_memory(desk, monkeypatch):
    """Run the normal decision and settlement entry points in an isolated exchange calendar."""
    from quantlab import realtime, research, runner

    class ExchangeClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff.astimezone(tz)

    cutoff = ExchangeClock(2025, 6, 16, 14, 30, tzinfo=research.SH)
    day = cutoff.date()
    sessions = list(pd.bdate_range(end=day + timedelta(days=10), periods=120).date)
    known = [d for d in sessions if d < day]
    monkeypatch.setattr(station, "now", lambda: cutoff)
    monkeypatch.setattr(research, "datetime", ExchangeClock)
    monkeypatch.setattr(runner, "_missed_cutoff", lambda d: False)
    monkeypatch.setattr(realtime, "snapshot", lambda: {"boards": 0})
    monkeypatch.setattr(station.ingest, "update_news", lambda **kwargs: 0)
    # Exercise the configured model-outage path without using a remote model.
    monkeypatch.setattr(station.llm, "research_queries", lambda *args: ([], "测试模式"))
    monkeypatch.setattr(station.llm, "research_opinion", lambda context: (None, "测试模式", None))
    table = db.qualified
    db.execute_many(
        f"INSERT INTO {table('trade_calendar')}(trade_date) VALUES (%s)", [(d,) for d in sessions]
    )
    bid = db.query_one(f"""INSERT INTO {table("board")} (code,name,kind) VALUES
        ('test','测试行业','industry') RETURNING board_id""")["board_id"]
    fid = db.query_one(f"""INSERT INTO {table("instrument")} (code,name,kind) VALUES
        ('000001','测试行业指数C','otc') RETURNING instrument_id""")["instrument_id"]
    db.execute(
        f"INSERT INTO {table('instrument_board')}(board_id,instrument_id) VALUES (%s,%s)",
        [bid, fid],
    )
    db.execute_many(
        f"""INSERT INTO {table("board_quote")} (board_id, trade_date, close, ingested_at)
        VALUES (%s,%s,%s,%s)""",
        [(bid, d, 100 + i, cutoff) for i, d in enumerate(known)],
    )
    db.execute_many(
        f"""INSERT INTO {table("instrument_nav")} (instrument_id,trade_date,nav,ingested_at)
        VALUES (%s,%s,%s,%s)""",
        [(fid, d, 1 + i * 0.001, cutoff) for i, d in enumerate(known)],
    )

    planned = runner.run_intraday()
    assert planned["status"] == "planned" and planned["action"] == "hold"
    first = station.dashboard(planned["report_id"])["decisions"][0]["payload"]
    assert first["research_targets"]["000001"] > 0 and not first["instructions"]
    assert runner.run_intraday()["skipped"]
    assert runner.run_settle(day)["settled"][0]["fills"] == 0
    assert not runner.run_settle(day)["settled"]
    evening = cutoff.replace(hour=20)
    db.execute(
        f"INSERT INTO {table('board_quote')}(board_id,trade_date,close,ingested_at) VALUES (%s,%s,250,%s)",
        [bid, day, evening],
    )
    db.execute(
        f"INSERT INTO {table('instrument_nav')}(instrument_id,trade_date,nav,ingested_at) VALUES (%s,%s,2,%s)",
        [fid, day, evening],
    )
    cutoff += timedelta(days=1)
    second = runner.run_intraday()
    assert second["status"] == "planned" and second["action"] == "pending"
    assert runner.run_settle(cutoff.date())["settled"][0]["status"] == "waiting_nav"
    db.execute(
        f"INSERT INTO {table('instrument_nav')}(instrument_id,trade_date,nav,ingested_at) VALUES (%s,%s,2.1,%s)",
        [fid, cutoff.date(), cutoff.replace(hour=20)],
    )
    assert runner.run_settle(cutoff.date())["settled"][0]["fills"] == 1
    # Both decisions' outcomes become visible only after their exchange-session closes.
    later = ExchangeClock(2025, 6, 24, 20, 30, tzinfo=research.SH)
    endpoints = [s for s in sessions if day < s <= later.date()]
    db.execute_many(
        f"""INSERT INTO {table("board_quote")} (board_id,trade_date,close,ingested_at)
        VALUES (%s,%s,%s,%s)""",
        [(bid, d, 251 + i, later) for i, d in enumerate(endpoints)],
    )
    assert research.settle_forecasts(cutoff) == 0
    assert research.settle_forecasts(later) == 4
    assert research.settle_forecasts(later) == 0
    report = station.dashboard(second["report_id"])
    assert report["decisions"][0]["payload"]["research_id"] == second["report_id"]
    assert report["orders"][0]["code"] == "000001"
    assert report["manager"]["holdings"][0]["code"] == "000001"
    assert len(research._memory(later)) == 2
    assert all(m["samples"] == 2 for m in research._memory(later))


def test_manager_tools_and_watch_use_the_research_clock(desk):
    from psycopg.types.json import Jsonb

    from quantlab import research_tools
    from quantlab.research import SH

    cutoff = datetime(2026, 9, 8, 11, 5, tzinfo=SH)
    then, later = cutoff - timedelta(hours=1), cutoff + timedelta(minutes=1)
    db.execute(
        f"INSERT INTO {db.qualified('agent_research')}(as_of,mode,payload) VALUES (%s,'rules',%s)",
        [then, Jsonb({"run_mode": "live", "watchlist": [{"board_id": 3}], "summary": "原研究"})],
    )
    assert station.watch_evidence(cutoff) is None
    db.execute_many(
        f"""INSERT INTO {db.qualified("news_item")}(source,title,published_at,ingested_at,content_hash)
            VALUES ('test','银行公告',%s,%s,%s)""",
        [(then, cutoff, "visible"), (then, later, "late_ingest"), (later, then, "future")],
    )
    assert "新闻" in station.watch_evidence(cutoff)
    subject = {
        "board_id": 3,
        "board": "银行",
        "news": [],
        "as_of": "2026-09-07",
        "source": "ths",
        "proxy_code": "",
        "evidence_ids": ["B3"],
    }
    requests = [
        {"tool": tool, "board_id": 3}
        for tool in ("read_news", "read_news", "inspect_sector", "recall", "sell")
    ]
    requests.append({"tool": "inspect_sector", "board_id": 4})
    calls = research_tools.investigate(requests, [subject], [], cutoff)
    assert [c["tool"] for c in calls] == ["read_news", "inspect_sector", "recall"]
    assert len(calls[0]["result"]["articles"]) == 1
    assert calls[2]["result"]["prior_research"][0]["summary"] == "原研究"
