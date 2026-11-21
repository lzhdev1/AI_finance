"""Current exchange session and its final paper instruction, in one database read."""

from datetime import datetime
from zoneinfo import ZoneInfo

from . import db
from .config import load_settings


def _state(trading, has_decision, clock, schedule):
    if has_decision:
        return "published"
    if trading is None:
        return "calendar_unknown"
    if not trading:
        return "closed"
    for boundary, state in (
        ("prepare", "scheduled"),
        ("review", "preparing"),
        ("target", "reviewing"),
        ("cutoff", "overdue"),
    ):
        if clock < schedule[boundary]:
            return state
    return "missed"


def today(as_of: datetime | None = None) -> dict:
    settings = load_settings()
    schedule = {
        "prepare": settings.schedule["decision_prepare"],
        "review": settings.schedule["intraday_decide"],
        "target": settings.schedule["decision_publish_by"],
        "cutoff": "15:00",
    }
    calendar = db.qualified("trade_calendar")
    # Both the workstation and website use the database clock. Never substitute the
    # previous session's instruction when today's card has not been committed.
    row = db.query_one(f"""
        WITH clock AS (SELECT coalesce(%s::timestamptz, statement_timestamp()) AS stamp),
        session AS (SELECT stamp, (stamp AT TIME ZONE %s)::date AS day FROM clock)
        SELECT s.stamp, s.day,
          EXISTS (SELECT FROM {calendar} WHERE trade_date=s.day) AS listed,
          EXISTS (SELECT FROM {calendar} WHERE trade_date<s.day) AS has_previous,
          (SELECT min(trade_date) FROM {calendar} WHERE trade_date>s.day) AS next_day,
          EXISTS (SELECT FROM {db.qualified("scheduler_state")} WHERE service='scheduler'
                  AND heartbeat_at BETWEEN s.stamp - interval '90 seconds' AND s.stamp) AS online,
          (SELECT to_jsonb(d) FROM {db.qualified("pod_decision")} d
           JOIN {db.qualified("pod")} p USING (pod_id)
           WHERE p.strategy_key='agent_manager' AND d.trade_date=s.day AND d.created_at<=s.stamp) AS decision
        FROM session s
    """, [as_of, settings.timezone])
    stamp = row["stamp"].astimezone(ZoneInfo(settings.timezone))
    day = row["day"]
    trading = None
    if row["listed"]:
        trading = True
    elif day.weekday() >= 5 or (row["has_previous"] and row["next_day"]):
        trading = False
    return {
        "as_of": stamp.isoformat(), "trade_date": day.isoformat(),
        "is_trade_day": trading, "next_trade_date": row["next_day"],
        "state": _state(trading, row["decision"] is not None, stamp.strftime("%H:%M"), schedule),
        "schedule": schedule, "scheduler_online": row["online"], "decision": row["decision"],
    }
