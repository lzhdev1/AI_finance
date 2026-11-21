from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb

from quantlab import behavior_followup, db
from test_station import desk  # noqa: F401

pytestmark = pytest.mark.usefixtures("desk")
SH = ZoneInfo("Asia/Shanghai")


def test_prior_plan_reads_only_visible_same_board_closes_and_verified_flows():
    table = db.qualified
    # Include a weekend and only exchange sessions, with no report on Monday.
    anchor, monday, checkpoint = (date(2026, 11, d) for d in (13, 16, 17))
    dates = [date(2026, 11, d) for d in (10, 11, 12, 13, 16, 17, 18)]
    db.execute_many(
        f"INSERT INTO {table('trade_calendar')}(trade_date) VALUES (%s)", [(d,) for d in dates]
    )
    bid = db.query_one(
        f"INSERT INTO {table('board')}(code,name,kind,source) VALUES ('a','银行','industry','ths') RETURNING board_id"
    )["board_id"]
    second = db.query_one(
        f"INSERT INTO {table('board')}(code,name,kind,source) VALUES ('b','银行','industry','eastmoney') RETURNING board_id"
    )["board_id"]

    def report(day, minute, regime, ordinal, run_mode="live"):
        stamp = datetime.combine(day, time(14, minute), SH)
        row = {
            "board_id": bid,
            "board": "银行",
            "source": "ths",
            "capital_flow": {"flow_source": "同花顺"},
            "behavior": {
                "regime": regime,
                "label": regime,
                "path": [
                    {
                        "day": ordinal,
                        "target_date": str(checkpoint),
                        "watch": "观察收盘后的量价反馈",
                        "confirm": "同源收盘资金流入",
                        "invalidate": "收盘净流出",
                    }
                ],
            },
        }
        return db.query_one(
            f"INSERT INTO {table('agent_research')}(as_of,mode,payload) VALUES (%s,'llm',%s) RETURNING report_id",
            [stamp, Jsonb({"run_mode": run_mode, "watchlist": [row]})],
        )["report_id"]

    first = report(anchor, 30, "demand", 2)
    report(anchor, 45, "supply", 2)  # Same anchor reissue cannot rewrite the first regime.
    report(monday, 25, "rotation", 1, "offline")
    report(monday, 30, "insufficient", 1)  # Prefer the still-relevant substantive older plan.
    report(checkpoint, 10, "supply", 1)  # Same-day report with an impossible target date.
    now = datetime.combine(checkpoint, time(14, 30), SH)
    late = now.replace(hour=20)
    db.execute_many(
        f"INSERT INTO {table('board_quote')}(board_id,trade_date,close,source,ingested_at) VALUES (%s,%s,%s,%s,%s)",
        [
            (bid, anchor, 100, "ths", now - timedelta(days=1)),
            (bid, monday, 105, "ths", now - timedelta(hours=1)),
            (bid, checkpoint, 80, "ths", now - timedelta(minutes=1)),  # no final close before 15:00
            (second, checkpoint, 200, "eastmoney", now),
        ],
    )
    db.execute_many(
        f"INSERT INTO {table('board_fund_flow')}(board_id,trade_date,period,main_net,source,ingested_at) VALUES (%s,%s,'1d',%s,%s,%s)",
        [
            (bid, anchor, 120, "ths-close-verified", now),
            (bid, monday, 0, "ths-close-verified", now - timedelta(hours=1)),
            (bid, checkpoint, -120, "ths-close-verified", late),
        ],
    )
    pool = [{"board_id": bid}, {"board_id": second}]
    behavior_followup.attach(pool, now, checkpoint)
    card = pool[0]["prior_behavior"]
    assert card["prior_regime"] == "demand" and card["report_id"] == first and card["day"] == 2
    assert card["status"] == "partial" and card["observed_close_date"] == str(monday)
    assert card["realized_board_return"] == pytest.approx(0.05)
    assert card["net_yuan"] == 0 and card["flow_date"] == str(monday)
    assert pool[1]["prior_behavior"] is None

    behavior_followup.attach(pool, late + timedelta(minutes=1), checkpoint)
    card = pool[0]["prior_behavior"]
    assert card["status"] == "observed" and card["realized_board_return"] == pytest.approx(-0.2)
    assert card["net_yuan"] == -120
    db.execute(
        f"UPDATE {table('board_quote')} SET source='different-series' WHERE board_id=%s AND trade_date=%s",
        [bid, anchor],
    )
    db.execute(f"UPDATE {table('board_fund_flow')} SET source='ths' WHERE board_id=%s", [bid])
    behavior_followup.attach(pool, late + timedelta(minutes=2), checkpoint)
    assert pool[0]["prior_behavior"]["status"] == "missing_data"
    assert pool[0]["prior_behavior"]["realized_board_return"] is None
    assert pool[0]["prior_behavior"]["net_yuan"] is None

    # A first-day checkpoint before close must retain the plan with no 0% substitute.
    db.execute(
        f"DELETE FROM {table('agent_research')} WHERE (as_of AT TIME ZONE 'Asia/Shanghai')::date=%s",
        [anchor],
    )
    behavior_followup.attach(pool, now, checkpoint)
    assert pool[0]["prior_behavior"]["status"] == "pending_close"
    assert pool[0]["prior_behavior"]["realized_board_return"] is None
