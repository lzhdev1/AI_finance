from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from quantlab import llm, llm_meter


MODEL = "deepseek-v4.1-flash-1017"
BASE = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


def _measure(body, hour=10, *, model=MODEL, host=BASE):
    at = datetime(2026, 11, 21, hour, tzinfo=llm_meter.SH)
    return llm_meter.measure(host, model, body, at, at)


def test_beijing_price_tracks_cached_input_and_inclusive_reasoning_tokens(monkeypatch):
    monkeypatch.delenv("LLM_USAGE_PRICE_JSON", raising=False)
    body = {
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "prompt_tokens_details": {"cached_tokens": 250_000},
            "completion_tokens_details": {"reasoning_tokens": 20_000},
        }
    }
    peak = _measure(body, 8)
    assert peak["total_tokens"] == 1_100_000 and peak["reasoning_tokens"] == 20_000
    assert peak["amount"] == Decimal("2.31")
    assert peak["currency"] == "CNY" and peak["cost_basis"] == "estimated"
    assert peak["provider"] == "阿里云百炼" and peak["source_url"] == llm_meter.ALI_PRICE_URL
    assert "ws-example" not in str(peak)
    assert _measure(body, 22)["amount"] == Decimal("1.155")
    early = datetime(2026, 11, 20, 12, tzinfo=llm_meter.SH)
    assert llm_meter.measure(BASE, MODEL, body, early, early)["cost_basis"] == "unknown"


def test_missing_receipt_token_breakdown_or_price_never_becomes_free(monkeypatch):
    monkeypatch.delenv("LLM_USAGE_PRICE_JSON", raising=False)
    for value in ({}, {"usage": {"total_tokens": 100}}, {"usage": {"prompt_tokens": True}}):
        fields = _measure(value)
        assert fields["cost_basis"] == "unknown" and fields.get("amount") is None
    bill = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    fields = _measure(bill)
    assert fields["cached_tokens"] is None and fields["amount"] == Decimal("0.0006")
    assert "上限" in fields["price_note"]
    assert (
        _measure(bill, host="https://maas.aliyuncs.com.untrusted.test")["cost_basis"] == "unknown"
    )
    assert _measure(bill, model="another-model")["cost_basis"] == "unknown"
    assert _measure({"usage": {"total_tokens": 0, "cost": 0.0}})["cost_basis"] == "unknown"
    invoice = _measure({"usage": {"cost": {"amount": "0", "currency": "USD"}}})
    assert (invoice["amount"], invoice["currency"], invoice["cost_basis"]) == (0, "USD", "reported")


def test_private_rate_card_scoped_to_actual_endpoint_and_model(monkeypatch):
    monkeypatch.setenv(
        "LLM_USAGE_PRICE_JSON",
        '{"custom":{"host":"internal.local","currency":"EUR","input":2,"output":5}}',
    )
    fields = _measure(
        {"usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        host="https://internal.local/private/secret",
        model="custom",
    )
    assert (fields["amount"], fields["currency"]) == (Decimal("0.000011"), "EUR")
    assert "internal" not in str(fields) and "secret" not in str(fields)
    invoice = _measure(
        {"usage": {"cost": 0.00001, "cost_currency": "USD", "input_tokens": 12, "output_tokens": 3}}
    )
    assert invoice["cost_basis"] == "reported" and invoice["amount"] == Decimal("0.00001")


def test_records_each_transmitted_attempt_also_when_output_or_response_is_unusable(monkeypatch):
    monkeypatch.setattr(llm, "BASE_URL", BASE)
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    events = []
    monkeypatch.setattr(llm.usage, "record_event", lambda **event: events.append(event))
    responses = iter(
        [
            SimpleNamespace(status_code=429, json=lambda: {"error": "private detail"}),
            SimpleNamespace(
                status_code=200, json=lambda: {"usage": {"total_tokens": 8}, "choices": []}
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                    "usage": {
                        "prompt_tokens": 6,
                        "completion_tokens": 2,
                        "prompt_cache_hit_tokens": 0,
                    },
                },
            ),
        ]
    )
    monkeypatch.setattr(llm.requests, "post", lambda *args, **kwargs: next(responses))
    assert llm._post([], json_mode=True, model=MODEL, retries=2, operation="投研分析") == (
        "partial",
        "length",
    )
    assert [r["status"] for r in events] == ["error", "error", "success"]
    assert [r["total_tokens"] for r in events] == [None, 8, 8]
    assert events[0]["cost_basis"] == "unknown" and "HTTP 429" in events[0]["detail"]
    assert "private detail" not in str(events)
    assert events[2]["cached_tokens"] == 0 and "上限" in events[2]["detail"]


def test_ledger_error_cannot_reissue_successful_model_call(monkeypatch):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(
            status_code=200, json=lambda: {"choices": [{"message": {"content": "ok"}}]}
        )

    def fail(**kwargs):
        raise RuntimeError("storage error")

    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm.usage, "record_event", fail)
    assert llm._post([], json_mode=False) == ("ok", "")
    assert len(calls) == 1
