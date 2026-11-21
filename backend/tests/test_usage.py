from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from quantlab import db, usage
from test_station import desk  # noqa: F401

pytestmark = pytest.mark.usefixtures("desk")


def report(*, historical=False, tools=None):
    payload = {
        "run_mode": "historical_replay" if historical else "live",
        "evidence": {"research_tools": {"calls": tools or []}},
    }
    with db.transaction() as conn:
        row = db.query_one(
            f"INSERT INTO {db.qualified('agent_research')}(as_of,mode,payload) "
            "VALUES(now(),'llm',%s) RETURNING report_id",
            [Jsonb(payload)],
        )
        usage.attach_report(conn, row["report_id"])
        return row["report_id"]


def test_report_usage_includes_retries_and_keeps_money_by_evidence_and_currency():
    with usage.scope("agent_research", 5):
        usage.record_event(
            kind="llm",
            operation="投研",
            provider="模型",
            model="m",
            status="success",
            input_tokens=10,
            output_tokens=4,
            cached_tokens=3,
            reasoning_tokens=2,
            amount=Decimal("0.0000072"),
            currency="CNY",
            cost_basis="estimated",
        )
        usage.record_event(kind="llm", operation="复核", provider="模型", model="m", status="error")
        with usage.scope("news", 6, inherit=True):
            with usage.attempt(
                kind="search", provider="本地", operation="检索", cost_basis="no_charge"
            ):
                pass
        rid = report()
    with usage.scope("snapshot", 7):
        usage.record_event(
            kind="data",
            operation="行情",
            provider="数据服务",
            status="success",
            amount=Decimal("0"),
            currency="USD",
            cost_basis="reported",
        )
    state = usage.dashboard("all")
    assert state["total"] == state["summary"]["calls"] == 4
    assert state["summary"]["total_tokens"] == 14
    assert state["summary"]["unknown_cost_calls"] == state["summary"]["missing_token_calls"] == 1
    assert state["summary"]["costs"][0]["estimated_amount"] == "0.0000072"
    assert state["summary"]["costs"][1]["reported_amount"] == "0"
    assert state["summary"]["costs"][1]["estimated_amount"] is None
    filtered = usage.dashboard("1", rid, "all", 0, 2)
    assert filtered["total"] == 3 and len(filtered["events"]) == 2
    assert filtered["report_coverage"] == "tracked"
    assert all(e["run_id"] == 5 and e["report_id"] == rid for e in filtered["events"])
    assert usage.dashboard("all", rid, "llm", 1, 1)["events"][0]["total_tokens"] == 14
    day = datetime.now(usage.SH).date()
    db.execute(
        f"UPDATE {db.qualified('agent_usage_event')} SET occurred_at=%s WHERE kind='data'",
        [datetime.combine(day, datetime.min.time(), usage.SH) - timedelta(seconds=1)],
    )
    assert usage.dashboard("1")["total"] == 3


def test_report_without_events_old_report_and_rolled_back_job():
    with usage.scope("historical_replay"):
        modern = report(historical=True)
    legacy = report(
        tools=[{"tool": "read_news", "status": "empty"}, {"tool": "recall", "status": "ok"}]
    )
    result = usage.dashboard(report_id=modern)
    assert result["report_coverage"] == "tracked" and result["events"] == []
    assert result["summary"]["total_tokens"] is None
    old = usage.dashboard(report_id=legacy)
    assert old["report_coverage"] == "not_tracked"
    assert len(old["historical_tools"]) == 2 and old["total"] == 0
    with pytest.raises(RuntimeError), db.transaction(), usage.scope("historical_replay"):
        with usage.attempt(kind="llm", provider="模型", operation="投研"):
            raise RuntimeError()
    assert usage.replay_snapshot()["summary"]["unknown_cost_calls"] == 1
    assert usage.replay_snapshot()["tracked_reports"] == 1
    with pytest.raises(LookupError):
        usage.dashboard(report_id=999999)
