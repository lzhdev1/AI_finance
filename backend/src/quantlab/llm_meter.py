"""Normalized usage and per-request price estimates for the manager's text completions."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
ALI_PRICE_URL = "https://help.aliyun.cn/zh/model-studio/deepseek-v4-1-flash"
ALI_PRICE_CHECKED = date(2026, 11, 21)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _tokens(*values):
    return next((v for v in values if type(v) is int and 0 <= v <= 2**63 - 1), None)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    return n if n.is_finite() and 0 <= n <= 10**12 else None


def _currency(value):
    return value.upper() if isinstance(value, str) and re.fullmatch(r"[a-zA-Z]{3}", value) else None


def _billing_fields(body):
    used = _dict(body.get("usage"))
    input_count = _tokens(used.get("prompt_tokens"), used.get("input_tokens"))
    output_count = _tokens(used.get("completion_tokens"), used.get("output_tokens"))
    total = _tokens(used.get("total_tokens"))
    if total is None and input_count is not None and output_count is not None:
        total = _tokens(input_count + output_count)
    cached = _tokens(
        _dict(used.get("prompt_tokens_details")).get("cached_tokens"),
        _dict(used.get("input_tokens_details")).get("cached_tokens"),
        used.get("prompt_cache_hit_tokens"),
    )
    reasoning = _tokens(
        _dict(used.get("completion_tokens_details")).get("reasoning_tokens"),
        _dict(used.get("output_tokens_details")).get("reasoning_tokens"),
    )
    return dict(
        input_tokens=input_count,
        output_tokens=output_count,
        total_tokens=total,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _reported_cost(body):
    for part in (_dict(body.get("usage")), body):
        cost = part.get("cost")
        if isinstance(cost, dict):
            amount, currency = _number(cost.get("amount")), _currency(cost.get("currency"))
        else:
            amount = _number(cost)
            currency = _currency(part.get("cost_currency") or part.get("currency"))
        if amount is not None and currency:
            return dict(
                amount=amount,
                currency=currency,
                cost_basis="reported",
                price_note="模型服务在本次响应中返回的金额。",
            )
    return None


def _rates(host, model, started, ended):
    """Overrides are scoped to both the billing endpoint and the requested model."""
    raw = os.getenv("LLM_USAGE_PRICE_JSON", "").strip()
    if raw:
        try:
            rule = _dict(_dict(json.loads(raw)).get(model))
        except ValueError:
            return None
        if rule and rule.get("host") == host:
            currency = _currency(rule.get("currency"))
            standard, output = _number(rule.get("input")), _number(rule.get("output"))
            cached = _number(rule.get("cached_input", rule.get("input")))
            if currency and None not in (standard, cached, output):
                return (currency, standard, cached, output, "部署配置中的每百万 token 单价", None)
            return None
    if not (host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com")):
        return None
    if model != "deepseek-v4.1-flash-1017" or started.astimezone(SH).date() < ALI_PRICE_CHECKED:
        return None
    busy_start = 8 <= started.astimezone(SH).hour < 22
    busy_end = 8 <= ended.astimezone(SH).hour < 22
    factor = 2 if busy_start or busy_end else 1
    period = "忙时" if factor == 2 else "闲时"
    if busy_start != busy_end:
        period = "跨计价时段，取较高单价"
    note = f"阿里云百炼北京公开标价（{ALI_PRICE_CHECKED} 核对，{period}）"
    return (
        "CNY",
        Decimal(factor),
        Decimal("0.02") * factor,
        Decimal(4 * factor),
        note,
        ALI_PRICE_URL,
    )


def measure(url: str, model: str, response, started: datetime, ended: datetime) -> dict:
    host = urlsplit(url).hostname or ""
    provider = "兼容模型服务"
    if host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com"):
        provider = "阿里云百炼"
    elif host == "api.deepseek.com":
        provider = "DeepSeek"
    elif host == "api.openai.com":
        provider = "OpenAI"
    body = _dict(response)
    counts = _billing_fields(body)
    result = {"provider": provider, **counts}
    reported = _reported_cost(body)
    if reported:
        return {**result, **reported}
    rates = _rates(host, model, started, ended)
    input_count, output_count, cached = (
        counts[k] for k in ("input_tokens", "output_tokens", "cached_tokens")
    )
    if (
        rates is None
        or input_count is None
        or output_count is None
        or (cached is not None and cached > input_count)
    ):
        return {**result, "cost_basis": "unknown"}
    currency, standard, hit, output, note, link = rates
    note += f"，每百万 token 普通输入 {standard:f}、缓存命中 {hit:f}、输出 {output:f} {currency}"
    # Some compatible APIs omit the cache breakdown. Keep that count unknown and quote
    # an explicitly labelled ceiling using the more expensive of the two input rates.
    if cached is None and input_count and hit != standard:
        standard = max(standard, hit)
        note += "；缺少缓存命中明细，按较高输入单价估算上限"
    known_cached = cached or 0
    amount = (
        (input_count - known_cached) * standard + known_cached * hit + output_count * output
    ) / 1_000_000
    return {
        **result,
        "amount": amount,
        "currency": currency,
        "cost_basis": "estimated",
        "price_note": note + "，未扣除免费额度及优惠。",
        "source_url": link,
    }
