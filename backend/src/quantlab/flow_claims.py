"""Verify a live model's sector flow statements against the observations it actually cites."""

import re
import unicodedata
from decimal import Decimal

_FLOW = re.compile(
    r"净(?:流[入出]|买入|卖出|流量|额|占比)|(?:资金|主力).{0,9}(?:流入|流出|流向|回流|撤离|买入|卖出)|"
    r"资金流(?:为|呈)[正负]|吸金|流[入出].{0,10}\d+(?:\.\d+)?\s*(?:亿|万|元)"
)
_PREDICATE = re.compile(
    r"净流入|净流出|净买入|净卖出|流入|流出|回流|净额|净占比|资金流(?=[为呈][正负])"
)
_FIVE_DAYS = re.compile(r"(?:最近|近|过去)?(?:五|5)(?:个交易日|日)")
_PREFIX = re.compile(
    r"(?:该板块|本板块|板块|行业|当日|今日|今天|当日收盘|收盘|全天|日度|盘中|日内|截至|"
    r"当前|最新|当期|累计|获|获得|有|呈现|录得|显示|资金|主力|资金流向|资金流|同花顺|东方财富|[:：\s])*"
)
_CONDITION = re.compile(
    r"^(?:风险[为是]?[：:]?)?(?:若|如果|倘若|假如|一旦|如出现|须警惕未来|警惕未来|未来可能|可能出现)"
)
_QUANTITY = r"([+\-−]?\d+(?:\.\d+)?)\s*(万亿(?:元)?|亿(?:元)?|万(?:元)?|元|%)?"
_RANGE = re.compile(_QUANTITY + r"\s*(?:至|到|~|—|–|-)\s*" + _QUANTITY)
_SINGLE = re.compile(
    r"(不低于|不高于|至少|至多|超过|大于|高于|不足|小于|低于)?" + _QUANTITY + r"(以上|以下)?"
)
_SCALES = {"元": 1, "万": 10_000, "亿": 100_000_000, "万亿": 1_000_000_000_000, "%": 1}


def _amount(number, unit):
    key = unit.removesuffix("元") or "元"
    scale = Decimal(_SCALES[key])
    n = Decimal(number.replace("−", "-"))
    return n * scale, scale * Decimal(10) ** n.as_tuple().exponent / 2


def _quantity_matches(tail, net, pct, *, magnitude, ratio):
    # A single observation supports a point value, a threshold or a stated interval.
    tail = re.sub(
        r"^(?:金额为|金额|额|规模|净占比|占比|约为|约|为数|为|呈|达到|达|近|合计|共计|介于|计)+",
        "",
        tail,
    )
    tail = tail.removesuffix("之间").strip()
    if not tail:
        return True
    if tail in {"为正", "为负", "正", "负"}:
        return (net > 0) if tail.endswith("正") else (net < 0)
    interval = _RANGE.fullmatch(tail)
    single = _SINGLE.fullmatch(tail) if not interval else None
    if not interval and not single:
        return False
    if interval:
        lo, u1, hi, u2 = interval.groups()
        if not (u1 or u2) or ("%" in (u1, u2) and (u1 or u2) != (u2 or u1)):
            return False
        unit = u2 or u1
        low, _ = _amount(lo, u1 or unit)
        high, _ = _amount(hi, unit)
    else:
        compare, number, unit, suffix = single.groups()
        if not unit or (compare and suffix):
            return False
        target, half_tick = _amount(number, unit)
    if ratio != (unit == "%") or (ratio and pct is None):
        return False
    actual = Decimal(str(pct if ratio else net))
    if magnitude:
        actual = abs(actual)
    if interval:
        return low <= actual <= high
    if compare in ("至少", "不低于") or suffix == "以上":
        return actual >= target
    if compare in ("至多", "不高于") or suffix == "以下":
        return actual <= target
    if compare in ("超过", "高于", "大于"):
        return actual > target
    if compare in ("不足", "低于", "小于"):
        return actual < target
    return actual == target if target == 0 else abs(actual - target) <= half_tick


def _observations(row, refs, text, decision_day):
    bid = row["board_id"]
    daily = row.get("capital_flow") or {}
    spot = row.get("intraday") or {}
    intraday = spot.get("capital_flow") or {}
    five = bool(_FIVE_DAYS.search(text))
    net = daily.get("net_5d_yuan" if five else "net_yuan")
    named_source = next((s for s in ("东方财富", "同花顺") if s in text), None)
    if (
        f"G{bid}" in refs
        and daily.get("flow_status") == "available"
        and daily.get("flow_date") == row.get("as_of")
        and net is not None
        and (not named_source or named_source == daily.get("flow_source"))
        and not re.search(r"盘中|日内", text)
        and ("主力" not in text or daily.get("flow_source") == "东方财富")
        and (not re.search(r"今天|今日", text) or daily["flow_date"] == decision_day)
    ):
        yield net, daily.get("net_5d_pct" if five else "net_pct")
    if (
        not five
        and named_source != "同花顺"
        and f"I{bid}" in refs
        and intraday.get("net_yuan") is not None
        and intraday.get("source") == "东方财富板块行情"
        and not re.search(r"收盘|全天|日度", text)
        and spot.get("at", "")[:10] == decision_day
    ):
        yield intraday["net_yuan"], intraday.get("net_pct_points")


def supported(text, boards, refs, *, subject=None, risk=False, decision_day=None):
    """Unverifiable aggregates, multi-day trends and ambiguous subjects stay out of live reports."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    previous_flow = False
    for clause in re.split(r"[,。;；!?！？\n]", text):
        clause = clause.strip()
        if not _FLOW.search(clause):
            # Do not let splitting the sentence detach an unsupported amount from its flow fact.
            if previous_flow and re.match(
                r"^(?:其(?:中)?|该笔|金额|数额|规模|占比|约|合计|共|为|达|超|计|\s)*[+\-]?\d",
                clause,
            ):
                return False
            previous_flow = False
            continue
        previous_flow = True
        if (
            risk
            and _CONDITION.match(clause)
            and not re.search(r"实际|已|事实|显示|但是|但|尽管", clause)
        ):
            continue
        named = [r for r in boards if r.get("board") and r["board"] in clause]
        if named:
            longest = max(named, key=lambda r: len(r["board"]))
            if any(r["board"] not in longest["board"] for r in named):
                return False
            row = longest
            if subject and subject["board_id"] != row["board_id"]:
                return False
            clause = clause.replace(row["board"], "", 1)
        else:
            row = subject
        predicate = _PREDICATE.search(clause)
        if (
            not row
            or not predicate
            or not _PREFIX.fullmatch(_FIVE_DAYS.sub("", clause[: predicate.start()]))
        ):
            return False
        kind, tail = predicate.group(), clause[predicate.end() :].strip()
        direction = (
            1
            if kind in ("净流入", "净买入", "流入", "回流")
            else -1
            if kind in ("净流出", "净卖出", "流出")
            else 0
        )
        ratio = kind == "净占比" or "占比" in tail
        if not any(
            (not direction or direction * net > 0)
            and _quantity_matches(tail, net, pct, magnitude=bool(direction), ratio=ratio)
            for net, pct in _observations(row, refs, clause, decision_day)
        ):
            return False
    return True
