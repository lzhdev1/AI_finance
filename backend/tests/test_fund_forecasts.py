from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from psycopg.types.json import Jsonb

from quantlab import db, fund_forecasts, otc, pod_store, runner, station
from test_station import desk  # noqa: F401


SH = ZoneInfo("Asia/Shanghai")
pytestmark = pytest.mark.usefixtures("desk")


def add_report(stamp, cards):
    return db.query_one(
        f"INSERT INTO {db.qualified('agent_research')}(as_of,mode,payload) VALUES (%s,'test',%s) RETURNING report_id",
        [stamp, Jsonb({"fund_forecasts": cards})],
    )["report_id"]


def test_intraday_fund_prediction_waits_for_both_published_navs():
    table = db.qualified
    stamp = datetime(2025, 6, 13, 14, 30, tzinfo=SH)
    base = stamp.date()  # Friday
    closed = list(pd.bdate_range(end=base - timedelta(days=1), periods=140).date)
    future = list(pd.bdate_range(start=base + timedelta(days=1), periods=10).date)
    db.execute_many(
        f"INSERT INTO {table('trade_calendar')}(trade_date) VALUES (%s)",
        [(d,) for d in closed + [base] + future],
    )
    fid = db.query_one(
        f"INSERT INTO {table('instrument')}(code,name,kind) VALUES ('014028','基金C','otc') RETURNING instrument_id"
    )["instrument_id"]
    db.execute_many(
        f"INSERT INTO {table('instrument_nav')}(instrument_id,trade_date,nav,ingested_at) VALUES (%s,%s,%s,%s)",
        [(fid, d, 1 + k * 0.001, stamp) for k, d in enumerate(closed)]
        + [
            (fid, base, 2.0, stamp.replace(hour=20)),
            (fid, future[0], 2.2, datetime.combine(future[1], time(8), SH)),
        ],
    )
    watch = [{"board": "银行", "funds": [{"code": "014028"}, {"code": "test-missing"}]}]
    due = {1: future[0], 5: future[4]}
    cards = fund_forecasts.prepare(stamp, closed, base, due, watch, {"014028": 0.2}, {})
    one, five, missing_one, missing_five = cards
    assert one["status"] == five["status"] == "pending"
    assert one["direction"] == "up" and one["observed_nav"] == pytest.approx(1.139)
    assert one["observed_nav_date"] == str(closed[-1]) and one["observation_lag"] == 1
    assert one["base_nav"] is None and one["base_date"] == str(base)
    assert one["otc"]["confirm_date"] == str(future[0])
    assert one["otc"]["earliest_redeem_date"] == str(future[1])
    assert missing_one["status"] == missing_five["status"] == "insufficient"
    report_id = add_report(stamp, cards)
    fund_forecasts.persist(report_id, cards, stamp)
    future_stamp = datetime.combine(future[1], time(9), SH)
    assert fund_forecasts.settle(future_stamp - timedelta(hours=2)) == 0
    assert fund_forecasts.settle(future_stamp) == 1
    assert fund_forecasts.settle(future_stamp) == 0
    by_horizon = fund_forecasts.for_report(report_id)
    assert by_horizon[0]["hit"] is True and by_horizon[0]["realized_return"] == pytest.approx(0.1)
    assert by_horizon[0]["base_nav"] == 2 and by_horizon[0]["target_nav"] == 2.2
    assert by_horizon[1]["status"] == "pending" and by_horizon[2]["status"] == "insufficient"
    # The original forecast reappears unsolved when read at the earlier clock, even after settlement.
    reread = fund_forecasts.prepare(stamp, closed, base, due, watch, {}, {})
    assert reread[0]["status"] == "pending" and reread[0]["hit"] is None
    revised = [one | {"prob_up": 0.1, "direction": "down"}]
    second_id = add_report(stamp + timedelta(minutes=1), revised)
    fund_forecasts.persist(second_id, revised, stamp + timedelta(minutes=1))
    assert fund_forecasts.for_report(second_id)[0]["report_id"] == report_id
    assert fund_forecasts.for_report(second_id)[0]["prob_up"] == one["prob_up"]
    assert len(fund_forecasts.history(status="insufficient")["items"]) == 2
    assert fund_forecasts.history(horizon=1, status="scored")["items"][0]["hit"] is True
    assert db.query_one(f"SELECT count(*) n FROM {table('agent_board_forecast')}")["n"] == 0
    assert fund_forecasts.missing_nav_dates(future_stamp) == {}
    five_day_close = datetime.combine(due[5], time(15), SH)
    assert fund_forecasts.missing_nav_dates(five_day_close - timedelta(seconds=1)) == {}
    assert fund_forecasts.missing_nav_dates(five_day_close) == {"014028": due[5]}


def test_online_book_registers_lots_and_separates_receivable_from_nav(monkeypatch):
    table = db.qualified
    days = list(pd.bdate_range(start=date(2025, 6, 12), periods=16).date)
    db.execute_many(
        f"INSERT INTO {table('trade_calendar')}(trade_date) VALUES (%s)", [(d,) for d in days]
    )
    fid = db.query_one(
        f"INSERT INTO {table('instrument')} (code,name,kind) VALUES ('014028','银行C','otc') RETURNING instrument_id"
    )["instrument_id"]
    db.execute_many(
        f"INSERT INTO {table('instrument_nav')}(instrument_id,trade_date,nav,ingested_at) VALUES (%s,%s,1,%s)",
        [(fid, d, datetime.combine(d, time(20), SH)) for d in days],
    )
    pod_id = pod_store.create_pod(
        name="研究",
        strategy_key="agent_manager",
        thesis="",
        invalidation_rules=[],
        entry_evidence={},
        trade_date=days[1],
    )
    pod = pod_store.get_pod(pod_id)

    def plan(idx, targets):
        cutoff = datetime.combine(days[idx], time(14, 30), SH)
        monkeypatch.setattr(station, "now", lambda: cutoff)
        return runner.plan_pod(pod, days[idx], {}, {"targets": targets})

    assert plan(1, {"014028": 0.3})["pending"][0]["side"] == "buy"  # Friday
    runner.execute_pod(pod, days[1])
    assert plan(2, {})["pending"] == []  # Monday, rights registered
    runner.execute_pod(pod, days[2])
    sell_card = plan(3, {})  # Tuesday, redemption allowed
    assert sell_card["pending"][0]["shares"] == 30000
    result = runner.execute_pod(pod, days[3])
    gross = 30000
    net = gross * (1 - 0.015)
    assert result["snapshot"]["cash"] == pytest.approx(70000 + net)
    assert otc.cash_receivable(pod_id, days[10]) == pytest.approx(net)
    assert otc.cash_receivable(pod_id, days[11]) == 0
    assert plan(4, {})["payload"]["spendable_cash"] == pytest.approx(70000)
    assert pod_store.load_account(pod).cash == pytest.approx(70000 + net)
    assert otc.clock("unknown", days)["source_url"] is None
    assert otc.clock("unknown", days)["redemptions_available_date"] == str(days[1])


def test_five_day_fund_calibration_only_uses_disjoint_matured_raw_estimates():
    table = db.qualified
    dates = list(pd.bdate_range(start=date(2025, 6, 2), periods=17).date)
    stamp = datetime.combine(dates[-1], time(14, 30), SH)
    fid = db.query_one(
        f"INSERT INTO {table('instrument')}(code,name,kind) VALUES ('014028','银行C','otc') RETURNING instrument_id"
    )["instrument_id"]
    rid = add_report(stamp, [])
    evidence = Jsonb({"prob_up": 0.8, "raw_prob_up": 0.5})
    # First two intervals overlap; the most recent one touches the older on the close.
    db.execute_many(
        f"""INSERT INTO {table("agent_fund_forecast")}
        (report_id,instrument_id,issued_at,base_date,target_date,horizon_days,evidence,realized_return,scored_at)
        VALUES (%s,%s,%s,%s,%s,5,%s,.01,%s)""",
        [
            (
                rid,
                fid,
                datetime.combine(dates[start], time(14, 30), SH),
                dates[start],
                dates[start + 5],
                evidence,
                score,
            )
            for start, score in [
                (0, stamp),
                (1, stamp),
                (6, stamp),
                (10, stamp + timedelta(days=1)),
            ]
        ],
    )
    calibrated = fund_forecasts._calibration(["014028"], stamp)[fid][5]
    assert calibrated["days"] == 2
    assert calibrated["shift"] == pytest.approx(1 / 32)
