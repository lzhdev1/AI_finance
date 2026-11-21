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


def test_today_decision_keeps_a_previous_card_out_of_the_new_session(desk):
    from zoneinfo import ZoneInfo
    from quantlab import decision_status

    sh = ZoneInfo("Asia/Shanghai")
    friday = datetime(2026, 11, 20, 14, 51, tzinfo=sh)
    monday = friday + timedelta(days=3)
    calendar = db.qualified("trade_calendar")
    assert decision_status.today(monday)["state"] == "calendar_unknown"
    db.execute_many(f"INSERT INTO {calendar}(trade_date) VALUES (%s)",
                    [(friday.date(),), (monday.date(),)])
    manager = station.ensure_manager()
    db.execute(f"INSERT INTO {db.qualified('pod_decision')}(pod_id,trade_date,action,summary,created_at) "
               "VALUES (%s,%s,'hold','维持现金',%s)", [manager["pod_id"], friday.date(), friday])
    assert decision_status.today(friday - timedelta(minutes=1))["decision"] is None
    published = decision_status.today(friday)
    assert published["state"] == "published" and published["decision"]["summary"] == "维持现金"
    assert decision_status.today(friday + timedelta(days=1))["state"] == "closed"
    for hour, minute, expected in (
        (14, 44, "scheduled"), (14, 45, "preparing"), (14, 50, "reviewing"),
        (14, 55, "overdue"), (15, 0, "missed"),
    ):
        result = decision_status.today(monday.replace(hour=hour, minute=minute))
        assert result["state"] == expected
        assert result["decision"] is None


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
        f"INSERT INTO {t('trade_calendar')}(trade_date,ingested_at) VALUES (%s,%s)", [(d,cutoff) for d in sessions]
    )
    bid = db.query_one(
        f"INSERT INTO {t('board')}(code,name,kind,created_at) VALUES('bank','银行','industry',%s) RETURNING board_id", [cutoff]
    )["board_id"]
    fid = db.query_one(
        f"INSERT INTO {t('instrument')}(code,name,kind,created_at,updated_at) VALUES('014028','银行指数C','otc',%s,%s) RETURNING instrument_id", [cutoff,cutoff]
    )["instrument_id"]
    db.execute(
        f"INSERT INTO {t('instrument_board')}(board_id,instrument_id,updated_at) VALUES(%s,%s,%s)", [bid, fid,cutoff]
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
        (14, 30, True),
        (14, 49, True),
        (14, 50, False),
        (14, 55, False),
        (15, 0, True),
    ):
        stamp = datetime(2026, 11, 20, hour, minute, tzinfo=research.SH)
        monkeypatch.setattr(station, "now", lambda stamp=stamp: stamp)
        assert runner._missed_cutoff(stamp.date()) is blocked


def test_deadline_publishes_once_while_final_research_is_blocked(desk, monkeypatch):
    from threading import Event
    from quantlab import research, runner

    stamp = datetime(2026, 11, 20, 14, 50, tzinfo=research.SH)
    monkeypatch.setattr(station, "now", lambda: stamp)
    db.execute(f"INSERT INTO {db.qualified('trade_calendar')}(trade_date) VALUES (%s)", [stamp.date()])
    started, resume = Event(), Event()

    def slow_review():
        started.set()
        assert resume.wait(8)
        return {"id": 77}

    monkeypatch.setattr(station, "decision_research", slow_review)
    assert runner.run_intraday(deadline=True)["skipped"]
    with ThreadPoolExecutor(3) as pool:
        final = pool.submit(runner.run_intraday)
        assert started.wait(5)
        try:
            stamp = stamp.replace(minute=54)
            results = list(pool.map(lambda _: runner.run_intraday(deadline=True), range(2)))
            assert any(row.get("status") == "planned" for row in results)
        finally:
            resume.set()
        assert final.result()["status"] == "existing"
    rows = station.history("decisions")["items"]
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["publication"]["source"] == "protective_hold"
    assert payload["publication"]["published_at"] == stamp.isoformat()
    assert payload["planned_action"] == "cash" and payload["instructions"] == []
    stamp += timedelta(days=3)
    stamp = stamp.replace(hour=15, minute=0)
    assert runner.run_intraday(deadline=True)["skipped"]
    assert len(station.history("decisions")["items"]) == 1


@pytest.mark.parametrize("new_evidence", [False, True])
def test_deadline_revalidates_completed_draft_before_using_it(desk, monkeypatch, new_evidence):
    from quantlab import research, runner

    stamp = datetime(2026, 11, 20, 14, 54, tzinfo=research.SH)
    before = stamp.replace(minute=45)
    monkeypatch.setattr(station, "now", lambda: stamp)
    t = db.qualified
    days = list(pd.bdate_range(start=stamp.date(), periods=6).date)
    db.execute_many(f"INSERT INTO {t('trade_calendar')}(trade_date,ingested_at) VALUES (%s,%s)",
                    [(d,before) for d in days])
    inputs = {"news": {"cls": {"rows": 4}}, "snapshot": {"boards": 10}}
    draft = {"id": 12, "decision_source": "prepared_review", "run_mode": "live",
             "as_of": before.isoformat(), "mode": "rules", "targets": {}, "rationale": "维持现金",
             "forecast_window": {"base_date": str(days[0]), "next_session": str(days[1]), "fifth_session": str(days[5])},
             "decision_inputs": inputs}
    monkeypatch.setattr(station, "completed_decision_research", lambda *_: draft)
    monkeypatch.setattr(station, "final_refresh_inputs", lambda _: inputs)
    if new_evidence:
        db.execute(f"""INSERT INTO {t('news_item')}(source,published_at,ingested_at,title,content_hash)
                       VALUES ('cls',%s,%s,'新公告','new')""", [stamp,stamp])
    assert runner.run_intraday(deadline=True)["status"] == "planned"
    pub = station.history("decisions")["items"][0]["payload"]["publication"]
    assert pub["evidence_as_of"] == before.isoformat() and pub["reviewed_through"] == stamp.isoformat()
    assert pub["source"] == ("protective_hold" if new_evidence else "prepared_review")
    if new_evidence:
        assert "新闻" in pub["reason"]


def test_research_to_paper_fill_to_forecast_memory(desk, monkeypatch):
    """Run the normal decision and settlement entry points in an isolated exchange calendar."""
    from quantlab import realtime, research, runner

    class ExchangeClockMeta(type):
        def __instancecheck__(cls, item):
            return isinstance(item, datetime)

    class ExchangeClock(datetime, metaclass=ExchangeClockMeta):
        @classmethod
        def now(cls, tz=None):
            return cutoff.astimezone(tz)

    cutoff = ExchangeClock(2025, 6, 16, 14, 50, tzinfo=research.SH)
    day = cutoff.date()
    sessions = list(pd.bdate_range(end=day + timedelta(days=10), periods=120).date)
    known = [d for d in sessions if d < day]
    monkeypatch.setattr(station, "now", lambda: cutoff)
    monkeypatch.setattr(research, "datetime", ExchangeClock)
    monkeypatch.setattr(realtime, "snapshot", lambda: {"boards": 1, "ts": cutoff.isoformat()})
    def refresh_news(*, audit):
        audit["cls"] = {"rows": 1}
        return 0
    monkeypatch.setattr(station.ingest, "update_news", refresh_news)
    # Exercise the configured model-outage path without using a remote model.
    monkeypatch.setattr(station.llm, "research_queries", lambda *args: ([], "测试模式"))
    monkeypatch.setattr(station.llm, "research_opinion", lambda context: (None, "测试模式", None))
    table = db.qualified
    db.execute_many(
        f"INSERT INTO {table('trade_calendar')}(trade_date,ingested_at) VALUES (%s,%s)", [(d,cutoff) for d in sessions]
    )
    bid = db.query_one(f"""INSERT INTO {table("board")} (code,name,kind,created_at) VALUES
        ('test','测试行业','industry',%s) RETURNING board_id""", [cutoff])["board_id"]
    fid = db.query_one(f"""INSERT INTO {table("instrument")} (code,name,kind,created_at,updated_at) VALUES
        ('000001','测试行业指数C','otc',%s,%s) RETURNING instrument_id""", [cutoff,cutoff])["instrument_id"]
    db.execute(
        f"INSERT INTO {table('instrument_board')}(board_id,instrument_id,updated_at) VALUES (%s,%s,%s)",
        [bid, fid, cutoff],
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
    instruction = report["decisions"][0]["payload"]
    assert instruction["instructions"][0]["name"] == "测试行业指数C"
    assert instruction["planned_action"] == "open"
    assert instruction["publication"]["source"] == "final_review"
    assert instruction["publication"]["forecast_targets"]["1"] == str(endpoints[1])
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
