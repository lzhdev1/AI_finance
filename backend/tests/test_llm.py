"""叙述层的护栏测试：不联网，只验证"模型想乱写时会被拦住"。"""

from __future__ import annotations

from quantlab import llm


def test_numbers_are_extracted_ignoring_sign() -> None:
    assert llm._numbers("超额 +3.75%，回撤 -8.94%") == {3.75, 8.94}
    assert llm._numbers("没有数字的一段话") == set()


def test_parse_plain_json() -> None:
    raw = '{"narrative": "板块转弱，主动降仓。"}'
    assert llm._parse_narrative(raw) == "板块转弱，主动降仓。"


def test_parse_truncated_json() -> None:
    """推理型模型被 max_tokens 截断时，也要能把话说完整地捞出来。"""
    raw = '{"narrative": "板块转弱，主动降仓，等待趋势修复'
    assert llm._parse_narrative(raw) == "板块转弱，主动降仓，等待趋势修复"


def test_parse_returns_none_when_nothing_usable() -> None:
    assert llm._parse_narrative("完全不是 JSON") is None
    assert llm._parse_narrative('{"narrative": ""}') is None


def test_number_ban_rejects_narrative_with_digits() -> None:
    """模型写出数字就是越界——数字只能由系统标注。"""
    assert llm._numbers("仓位降到 50%，现金留 55%") != set()


def test_available_requires_full_config(monkeypatch) -> None:
    monkeypatch.setattr(llm, "BASE_URL", "https://example.com/v1")
    monkeypatch.setattr(llm, "API_KEY", "sk-x")
    monkeypatch.setattr(llm, "MODEL", "some-model")
    monkeypatch.setattr(llm, "ENABLED", True)
    assert llm.available() is True

    monkeypatch.setattr(llm, "API_KEY", "")
    assert llm.available() is False

    monkeypatch.setattr(llm, "API_KEY", "sk-x")
    monkeypatch.setattr(llm, "ENABLED", False)
    assert llm.available() is False


def test_deadline_bounds_each_model_attempt_and_is_local_to_the_decision(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    monkeypatch.setattr(llm.time, "time", lambda: 100.0)
    timeouts = []

    def post(url, **options):
        timeouts.append(options["timeout"])
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(llm.requests, "post", post)
    with llm.research_deadline(datetime.fromtimestamp(103, timezone.utc)):
        assert llm._post([], json_mode=True)[0] == "{}"
    with llm.research_deadline(datetime.fromtimestamp(100, timezone.utc)):
        assert llm._post([], json_mode=True)[0] is None
    assert llm._post([], json_mode=True)[0] == "{}"
    assert timeouts == [min(3, llm.TIMEOUT), llm.TIMEOUT]


def test_truncated_research_retries_with_smaller_evidence_within_configured_budget(monkeypatch):
    import json
    from types import SimpleNamespace

    from quantlab import config

    monkeypatch.setattr(llm, "BASE_URL", "https://example.test")
    monkeypatch.setattr(llm, "API_KEY", "test")
    monkeypatch.setenv("LLM_MODEL_DEEP", "unit-test")
    monkeypatch.setenv("LLM_MAX_TOKENS", "450")
    monkeypatch.setattr(
        config, "load_settings", lambda: SimpleNamespace(llm=SimpleNamespace(enabled=True))
    )
    article = {
        "id": 23,
        "title": "一条共享标题",
        "excerpt": "观察消息面的实际变化",
        "source": "测试",
    }
    base = {
        "score": 89.0,
        "momentum_5": 0.01,
        "momentum_20": 0.02,
        "flow_ratio": None,
        "proxy_code": "",
        "intraday": None,
        "fund_status": "可用",
        "forecasts": [],
        "outlooks": [],
        "funds": [{"code": "C"}],
        "forecast_factor": 0.5,
        "memory_factor": 1.0,
        "news": [article],
    }
    context = {
        "candidates": [{**base, "board_id": n, "board": str(n)} for n in range(7)],
        "news": [article],
        "as_of": "2026-11-20T14:30:00+08:00",
        "research_date": "2026-11-19",
        "historical_replay": True,
        "recent_experience": [],
        "paper_account": {"holdings": [], "recent_orders": [], "prior_research": []},
        "forecast_method": "非重叠历史频率",
    }
    calls = []

    def post(messages, **kwargs):
        calls.append((messages[-1]["content"], kwargs["max_tokens"]))
        return ('{"unfinished":', "length") if len(calls) == 1 else ('{"watch":[]}', "stop")

    monkeypatch.setattr(llm, "_post", post)
    value, status, _ = llm.research_opinion(context)
    assert value["watch"] == [] and "重试" in status
    assert len(calls) == 2 and all(budget == 450 for _, budget in calls)
    assert len(calls[1][0]) < len(calls[0][0])
    assert all(text.count(article["title"]) == 1 for text, _ in calls)
    assert len(json.loads(calls[1][0])["candidates"]) == 2

    calls.clear()

    def always_truncated(messages, **kwargs):
        calls.append(kwargs["max_tokens"])
        return '{"cut":', "length"

    monkeypatch.setattr(llm, "_post", always_truncated)
    value, status, _ = llm.research_opinion(context)
    assert value is None and "精简重试仍达到输出上限" in status and calls == [450, 450]

    calls.clear()
    live = {**context, "historical_replay": False}
    value, status, _ = llm.research_opinion(live)
    assert value is None and status == llm.LIVE_TRUNCATED and calls == [450]

    from quantlab import research

    row = {
        **base,
        "board_id": 81,
        "board": "银行",
        "as_of": "2026-11-19",
        "evidence_ids": ["B81", "G81"],
        "capital_flow": {
            "flow_status": "available",
            "flow_source": "同花顺",
            "flow_date": "2026-11-19",
            "net_yuan": 125_000_000,
        },
    }
    peer = {**base, "board_id": 86, "board": "证券", "funds": [], "evidence_ids": ["B86"]}
    live.update(review_draft=True, candidates=[row, peer])
    live["forecast_window"] = {
        "base_date": "2026-11-20", "next_session": "2026-11-23", "fifth_session": "2026-11-27",
    }
    live["evidence_coverage"] = {"boards_screened": 45}
    live["news"] = [{**article, "published_at": "2026-11-20T14:42:00+08:00"}]
    row["news"] = peer["news"] = live["news"]
    live["paper_account"] = {
        **live["paper_account"],
        "prior_research": [
            {"report_id": n, "as_of": f"2026-11-20T14:{n}:00+08:00", "summary": "相同看法" if n > 1 else "观点不同"}
            for n in range(16, 0, -1)
        ],
    }
    selection = {
        "stance": "reduced",
        "watch": [{"board_id": 81, "intent": "allocate", "driver": "G81"}],
        "risk": "trend_reversal",
    }
    prompts = []

    def choose(messages, **kwargs):
        assert kwargs["concise_json"] and kwargs["max_tokens"] == 450 and kwargs["retries"] == 0
        prompts.append(json.loads(messages[-1]["content"]))
        return json.dumps(selection), "stop"

    monkeypatch.setattr(llm, "_post", choose)
    value, status, _ = llm.research_opinion(live)
    assert len(prompts) == 1 and "由已引用证据生成" in status
    assert prompts[0]["forecast_window"] == live["forecast_window"]
    assert [g["report_ids"] for g in prompts[0]["portfolio"]["prior_analyses"]] == [list(range(16, 1, -1)), [1]]
    assert value["thesis"] == prompts[0]["candidates"][0]["drivers"]["G81"]
    assert research._selection([row, peer], value, decision_day="2026-11-20")
    for invalid in [
        {"board_id": 86, "intent": "observe", "driver": "G81"},
        {"board_id": 81, "intent": "observe", "driver": "G86"},
        {"board_id": 86, "intent": "allocate", "driver": "B86"},
        {"board_id": 800, "intent": "allocate", "driver": "B800"},
    ]:
        selection["watch"] = [invalid]
        assert llm.research_opinion(live)[0] is None
