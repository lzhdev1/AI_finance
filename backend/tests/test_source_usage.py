from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from quantlab import board_spots, realtime, research_tools, sources, usage


@pytest.fixture
def events(monkeypatch):
    rows = []

    def collect(**event):
        assert usage.active()
        rows.append(event)

    monkeypatch.setattr(usage, "record_event", collect)
    with usage.scope("test", run_id=7):
        yield rows


def test_empty_local_search_counts_and_repeated_requests_are_deduplicated(monkeypatch, events):
    monkeypatch.setattr(research_tools.db, "query", lambda *_: [])
    as_of = datetime(2026, 11, 20, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    board = {"board_id": 2, "board": "银行", "proxy_code": "", "news": [], "evidence_ids": []}
    calls = [{"board_id": 2, "tool": t} for t in ("read_news", "read_news", "recall", "inspect_sector")]
    result = research_tools.investigate(calls, [board], [], as_of)
    assert [r["status"] for r in result] == ["empty", "empty", "ok"]
    assert [(r["kind"], r["status"], r["cost_basis"]) for r in events] == [
        ("search", "success", "no_charge"), ("tool", "success", "no_charge"),
        ("tool", "success", "no_charge"),
    ]


def test_nav_history_and_fallback_are_two_unpriced_requests(monkeypatch, events):
    monkeypatch.setattr(sources.THROTTLE, "wait", lambda: None)

    def get(url, **kwargs):
        if "/f10/" in url:
            raise requests.Timeout("private request diagnostic")
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text=(
            'var Data_netWorthTrend = [{"x":1767628800000,"y":1.1}];var Data_ACWorthTrend = [];'
        ))

    monkeypatch.setattr(requests, "get", get)
    assert not sources.fetch_fund_nav("000001", since=date(2026, 1, 6)).empty
    assert [e["status"] for e in events] == ["error", "success"]
    assert [e["operation"] for e in events] == ["读取基金净值明细", "读取基金净值走势"]
    assert all(e["kind"] == "data" and e.get("amount") is None for e in events)
    assert "private request diagnostic" not in str(events)


def test_adapter_usage_is_counted_at_operation_boundary_on_error(monkeypatch, events):
    monkeypatch.setattr(sources.THROTTLE, "wait", lambda: None)

    def throttled():
        raise RuntimeError("RateLimit")

    with pytest.raises(sources.RateLimited):
        sources._call(throttled, "财联社", "抓取财经电报")
    entry, = events
    assert entry["status"] == "error" and entry["operation"] == "抓取财经电报"
    assert "适配器" in entry["detail"] and entry.get("amount") is None


def test_failed_intraday_requests_follow_worker_scope(monkeypatch, events):
    as_of = datetime(2026, 11, 20, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(board_spots, "ths_shortlist", lambda _: [
        {"kind": "industry", "code": "881121", "name": "银行"},
        {"kind": "concept", "code": "309049", "name": "共封装光学"},
    ])

    def get(*args, **kwargs):
        raise TimeoutError("private request diagnostic")

    monkeypatch.setattr(realtime.urllib.request, "urlopen", get)
    assert board_spots.fetch_ths(as_of, realtime._get).empty
    assert {e["operation"] for e in events} == {"读取板块分时行情", "核对概念板块行情代码"}
    assert all(e["provider"] == "同花顺" and e["status"] == "error" for e in events)
    assert "private request diagnostic" not in str(events)
