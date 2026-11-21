"""Point-in-time sector opportunities and a cash-funded OTC trial position."""

import json
from collections import defaultdict
from datetime import time, timedelta
from math import isfinite, prod
from statistics import mean, median, stdev

import pandas as pd

from . import config, db, execution
from .behavior import _day, _stamp

HISTORY = 126
WINDOW = 21
HORIZON = 5
MAX_WEIGHT = 0.05
MIN_WEIGHT = 0.02  # Execution ignores new target deltas below two percentage points.
ENTRY_STATES = {"breakout", "retest"}


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if isfinite(n) else None


def _sessions(closed_sessions, cutoff):
    return sorted(
        {
            _day(d)
            for d in closed_sessions
            if _day(d) < cutoff.date() or (_day(d) == cutoff.date() and cutoff.time() >= time(15))
        }
    )[-HISTORY:]


def load_bars(board_ids, closed_sessions, as_of, *, replay=False):
    cutoff = _stamp(as_of)
    days = _sessions(closed_sessions, cutoff)
    ids = list(set(board_ids))
    if not ids or not days:
        return {}
    rows = db.query(
        f"""SELECT q.board_id,q.trade_date,q.source,q.ingested_at,q.high::float,q.low::float,
                   q.close::float,q.amount::float,ix.pct_chg::float benchmark_pct
            FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
            LEFT JOIN {db.qualified("index_quote")} ix ON ix.index_code='000300'
              AND ix.trade_date=q.trade_date AND (%s OR ix.ingested_at<=%s)
            WHERE q.board_id=ANY(%s) AND q.trade_date=ANY(%s) AND (%s OR q.ingested_at<=%s)
              AND q.source=b.source ORDER BY q.board_id,q.trade_date""",
        [replay, cutoff, ids, days, replay, cutoff],
    )
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["board_id"]].append(row)
    return dict(grouped)


def load_navs(codes, closed_sessions, as_of, *, replay=False):
    cutoff = _stamp(as_of)
    # Historical archives lack original publication clocks. In live research a
    # same-session NAV is admissible only after the close and actual receipt.
    days = _sessions(closed_sessions, cutoff)
    if replay or cutoff.time() <= time(15):
        days = [d for d in days if d < cutoff.date()]
    days = days[-WINDOW:]
    if not codes or not days:
        return {}
    rows = db.query(
        f"""SELECT i.code,n.trade_date,n.nav::float FROM {db.qualified("instrument_nav")} n
            JOIN {db.qualified("instrument")} i USING(instrument_id)
            WHERE i.code=ANY(%s) AND n.trade_date=ANY(%s) AND n.nav>0
              AND (%s OR n.ingested_at<=%s)
              AND (n.trade_date<>%s OR n.ingested_at >=
                   ((n.trade_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'))
            ORDER BY i.code,n.trade_date""",
        [list(codes), days, replay, cutoff, cutoff.date()],
    )
    grouped = defaultdict(dict)
    for row in rows:
        grouped[row["code"]][row["trade_date"]] = row["nav"]
    return {code: pd.Series(points, dtype=float) for code, points in grouped.items()}


def disclosed_fees(observed, codes, day):
    """Merge a dated snapshot with public terms visible on the application date."""
    result = {c: observed[c] for c in codes if c in observed}
    path = config.CONFIG_DIR / "opportunity_fees.json"
    if day is None or not path.exists():
        return result
    catalog = json.loads(path.read_text(encoding="utf-8"))["funds"]
    as_at = _day(day).isoformat()
    for code in codes:
        if result.get(code, {}).get("buy") is not None and result[code].get("sell"):
            continue
        # The disclosure carries a calendar day, without a publication time.
        eligible = [
            r
            for r in catalog.get(code, [])
            if r.get("source")
            and r["published_on"] < as_at
            and r["effective_from"] <= as_at
            and (not r.get("effective_until") or as_at <= r["effective_until"])
        ]
        if eligible:
            record = max(eligible, key=lambda r: (r["effective_from"], r["published_on"]))
            result[code] = dict(
                record,
                sell=[
                    (bound if bound is not None else 999999, rate) for bound, rate in record["sell"]
                ],
            )
    return result


def load_fees(codes, *, day=None):
    """Read observed rates. Never substitute the ledger's fallback fee brackets."""
    if not codes:
        return {}
    rows = db.query(
        f"""SELECT i.code,f.side,f.holding_days_max,f.rate::float
            FROM {db.qualified("instrument_fee")} f JOIN {db.qualified("instrument")} i
            USING(instrument_id) WHERE i.code=ANY(%s) ORDER BY i.code,f.side,f.holding_days_max""",
        [list(codes)],
    )
    grouped = defaultdict(dict)
    for row in rows:
        book = grouped[row["code"]]
        if row["side"] == "buy":
            book["buy"] = row["rate"]
        else:
            book.setdefault("sell", []).append((row["holding_days_max"], row["rate"]))
    return disclosed_fees(grouped, codes, day)


def fee_cost(fee_schedule, day, horizon_end):
    """Break-even gross return including subscription and redemption fees."""
    if not fee_schedule or not day or not horizon_end:
        return None
    days = (_day(horizon_end) - _day(day)).days
    buy = _number(fee_schedule.get("buy"))
    if buy is None or not 0 <= buy < 1 or days <= 0:
        return None
    for limit, rate in sorted(fee_schedule.get("sell") or []):
        if days < limit:
            sell = _number(rate)
            if sell is not None and 0 <= sell < 1:
                return (1 + buy) / (1 - sell) - 1
            break
    return None


def _bar(row, cutoff, replay):
    if not row or not row.get("source"):
        return None
    if not replay and (not row.get("ingested_at") or _stamp(row["ingested_at"]) > cutoff):
        return None
    high, low, close, amount = (_number(row.get(k)) for k in ("high", "low", "close", "amount"))
    if None in (high, low, close, amount) or not (0 < low <= close <= high and amount > 0):
        return None
    return (row["source"], high, low, close, amount, _number(row.get("benchmark_pct")))


def _pattern(window):
    if len(window) != WINDOW or not all(window) or any(b[0] != window[-1][0] for b in window):
        return None
    if any(b[5] is None or b[5] <= -100 for b in window[-HORIZON:]):
        return None
    _, high, low, close, amount, _ = window[-1]
    prices = [b[3] for b in window]
    vol = stdev([p / prev - 1 for prev, p in zip(prices, prices[1:])])
    amount_ratio = amount / median(b[4] for b in window[:-1])
    location = (close - low) / (high - low) if high > low else 0.5
    ret = close / prices[-2] - 1
    ret5 = close / prices[-6] - 1
    relative = ret5 - (prod(1 + b[5] / 100 for b in window[-5:]) - 1)
    prior_high = max(b[1] for b in window[-11:-1])
    mean10 = mean(prices[-11:-1])
    recent_floor = min(b[2] for b in window[-6:-1])
    run = prices[-2] / prices[-7] - 1
    extended = ret > max(0.055, 2.8 * vol) or ret5 > max(0.12, 5 * vol)
    state = "watch"
    support = recent_floor
    if close < recent_floor and location < 0.4:
        state = "breakdown"
    elif run >= 0.025 and amount_ratio >= 1.3 and location <= 0.30 and ret <= 0:
        state = "distribution"
    elif (
        not extended
        and close > prior_high
        and ret > 0
        and amount_ratio >= 1.25
        and location >= 0.65
        and relative >= 0.005
    ):
        state = "breakout"
        support = prior_high * (1 - min(0.025, max(0.008, vol)))
    elif (
        not extended
        and mean10 > mean(prices[:-1])
        and low <= mean10 * 1.01
        and low >= mean10 * 0.965
        and close >= mean10
        and prior_high / low >= 1.025
        and ret > 0
        and location >= 0.65
        and amount_ratio >= 1.0
        and relative >= 0.005
    ):
        state = "retest"
        support = low * 0.995
    metrics = {
        "close": close,
        "amount_ratio": amount_ratio,
        "close_location": location,
        "relative_5d": relative,
        "return_1d": ret,
        "return_5d": ret5,
        "volatility_20d": vol,
        "prior_high_10d": prior_high,
        "mean_10d": mean10,
        "prior_low_5d": recent_floor,
        "extended": extended,
    }
    return state, support, metrics


def _analogs(bars, state):
    # Signal at close i is visible for application at i+1's unknown NAV. Learn
    # only from disjoint five-session outcomes whose exit has already closed.
    observations = []
    i = len(bars) - HORIZON - 2
    while i >= WINDOW - 1:
        result = _pattern(bars[i - WINDOW + 1 : i + 1])
        future = bars[i + 1 : i + HORIZON + 2]
        if (
            result
            and result[0] == state
            and all(future)
            and all(b[0] == bars[i][0] for b in future)
        ):
            entry = future[0][3]
            observations.append(
                (future[-1][3] / entry - 1, min(b[2] / entry - 1 for b in future[1:]))
            )
            i -= HORIZON + 1
        else:
            i -= 1
    return {
        "analog_count": len(observations),
        "analog_mean_5d": mean(x[0] for x in observations) if observations else None,
        "analog_hit_rate": mean(x[0] > 0 for x in observations) if observations else None,
        "analog_median_adverse": median(min(0, x[1]) for x in observations)
        if observations
        else None,
    }


def _intraday_reading(intraday, cutoff):
    if not intraday or intraday.get("source") not in ("东方财富板块行情", "同花顺板块分时"):
        return None
    if not intraday.get("at") or not intraday.get("captured_at"):
        return None
    at, captured = _stamp(intraday["at"]), _stamp(intraday["captured_at"])
    if (
        at.date() != cutoff.date()
        or at < cutoff - timedelta(minutes=30)
        or not at <= captured <= cutoff
    ):
        return None
    return _number(intraday.get("return"))


def assess(bars, *, closed_sessions, as_of, flow=None, intraday=None, replay=False):
    cutoff = _stamp(as_of)
    days = _sessions(closed_sessions, cutoff)
    result = {
        "state": "insufficient",
        "signal_date": days[-1].isoformat() if days else None,
        "eligible": False,
        "evidence": [],
        "metrics": {},
        "support": None,
        "invalidation": "等待连续行情及同日沪深300对照。",
        "score": 0.0,
        "probe_weight": 0.0,
    }
    dates = set(days)
    indexed = {
        _day(b["trade_date"]): _bar(b, cutoff, replay)
        for b in bars
        if _day(b["trade_date"]) in dates
    }
    visible = [indexed.get(d) for d in days]
    pattern = _pattern(visible[-WINDOW:])
    if not pattern:
        result["evidence"].append("连续21个交易日的同源量价或最近5日基准不齐，暂停判定。")
        return result
    state, support, metrics = pattern
    explanation = {
        "breakout": "放量突破前10个交易日高点，可能有增量承接，也可能只是被动调仓。",
        "retest": "上行趋势回踩10日均线后收复，承接可能恢复，仍需后续收盘确认。",
        "distribution": "上涨后放量且收在当日区间低位，获利兑现和集中赎回都能产生这种表现。",
        "breakdown": "收盘跌破此前5个交易日低点，承接走弱。",
        "watch": "涨幅过于集中，等待回踩。"
        if metrics["extended"]
        else "量价尚未同时达到试探条件。",
    }[state]
    evidence = [
        explanation,
        f"成交额为此前20日中位数的 {metrics['amount_ratio']:.2f} 倍，"
        f"收盘位于当日区间 {metrics['close_location']:.0%}，5日相对沪深300 {metrics['relative_5d']:+.2%}。",
    ]
    verified = (
        (flow or {}).get("flow_status") == "available"
        and (flow or {}).get("flow_source")
        in ("同花顺", "东方财富", "ths-close-verified", "eastmoney-close-verified")
        and str(flow.get("flow_date")) == result["signal_date"]
    )
    net = _number(flow.get("net_yuan")) if verified else None
    flow_pct = _number(flow.get("net_pct")) if net is not None else None
    spot = _intraday_reading(intraday, cutoff)
    spot_flow = (
        (intraday.get("capital_flow") or {})
        if spot is not None and intraday.get("source") == "东方财富板块行情"
        else {}
    )
    spot_pct = _number(spot_flow.get("net_pct_points"))
    spot_net = _number(spot_flow.get("net_yuan"))
    spot_alert = spot is not None and abs(spot) >= max(0.02, 2 * metrics["volatility_20d"])
    metrics.update(
        flow_net_yuan=net,
        flow_net_pct_points=flow_pct,
        intraday_return=spot,
        intraday_flow_net_yuan=spot_net,
        intraday_flow_net_pct_points=spot_pct,
        intraday_at=intraday.get("at") if spot is not None else None,
        intraday_alert=spot_alert,
    )
    evidence.append(
        f"同日已核验收盘净额 {net / 1e8:+.2f} 亿元。"
        if net is not None
        else "未取得同日已核验收盘资金净额，承接强弱仅用价格与成交额判断。"
    )
    if spot is not None:
        evidence.append(f"{intraday['source']} {intraday['at']} 板块涨跌 {spot:+.2%}。")
        if spot_alert and state == "watch":
            evidence.append(
                "盘中涨跌幅的绝对值已达20日波动率的两倍且至少2%，等待完整成交额和收盘位置验证。"
            )
        if spot_net is not None:
            evidence.append(
                f"同期盘中主力净额估算 {spot_net / 1e8:+.2f} 亿元"
                + (f"，净占比 {spot_pct:+.2f}%。" if spot_pct is not None else "。")
            )
    eligible = state in ENTRY_STATES
    if eligible:
        metrics.update(_analogs(visible, state))
        n = metrics["analog_count"]
        evidence.append(
            f"已走完5日窗口的独立同形态样本 {n} 次"
            + (
                f"，次日收盘起算平均 {metrics['analog_mean_5d']:+.2%}，"
                f"期间最深下探中位数 {metrics['analog_median_adverse']:.2%}。"
                if n
                else "。"
            )
        )
        if n >= 5 and metrics["analog_mean_5d"] <= 0:
            eligible = False
            evidence.append("同形态已兑现的平均5日回报为负或为零，暂停进场。")
        if net is not None and net < 0 and flow_pct is not None and flow_pct <= -2:
            eligible = False
            evidence.append("同日收盘出现有幅度的资金净流出，与量价信号冲突。")
        if spot is not None:
            if (
                (1 + spot) * metrics["close"] < support
                or spot <= -max(0.02, metrics["volatility_20d"])
                or (
                    spot_net is not None
                    and spot_net < 0
                    and spot_pct is not None
                    and spot_pct <= -2
                )
            ):
                eligible = False
                evidence.append("截点前盘中回落或净流出达到否决条件。")
    weight = (
        MAX_WEIGHT if net is not None and net > 0 and metrics.get("analog_count", 0) >= 5 else 0.03
    )
    score = (
        100 * metrics["relative_5d"] + min(3, metrics["amount_ratio"]) + metrics["close_location"]
    )
    result.update(
        state=state,
        evidence=evidence,
        metrics=metrics,
        eligible=eligible,
        support=round(support, 5),
        score=round(score, 4),
        probe_weight=weight if eligible else 0.0,
        invalidation=f"后续已公布收盘跌破板块 {support:.2f} 点，或出现放量兑现/破位，撤销试探。",
    )
    return result


def probe_weight(signal, fund_navs, *, closed_sessions, round_trip_fee):
    """Check observed fund behaviour, costs and a 10 bp daily portfolio volatility budget."""
    if not signal.get("eligible") or signal.get("state") not in ENTRY_STATES:
        return 0.0, "板块形态未确认或已有相反证据。"
    cost = _number(round_trip_fee)
    if cost is None or cost < 0:
        return 0.0, "缺少可核对的申购费或计划持有期赎回费。"
    signal_day = _day(signal["signal_date"])
    days = sorted({_day(d) for d in closed_sessions if _day(d) <= signal_day})[-WINDOW:]
    points = {} if fund_navs is None else {_day(d): _number(v) for d, v in fund_navs.items()}
    values = [points.get(d) for d in days]
    if len(days) < WINDOW or days[-1] != signal_day or any(v is None or v <= 0 for v in values):
        return 0.0, "基金自身连续21个交易日的单位净值不齐。"
    ret5 = values[-1] / values[-6] - 1
    if ret5 <= cost or values[-1] < mean(values[-10:]):
        return (
            0.0,
            f"基金5日已实现涨幅 {ret5:+.2%}、10日均线位置与往返成本 {cost:.2%} 未共同支持试探。",
        )
    analog = signal.get("metrics") or {}
    if analog.get("analog_count", 0) >= 5 and analog["analog_mean_5d"] <= cost:
        return 0.0, f"历史同形态板块平均涨幅未覆盖基金往返成本 {cost:.2%}。"
    moves = [b / a - 1 for a, b in zip(values, values[1:])]
    vol = max(stdev(moves), _number(analog.get("volatility_20d")) or 0.0)
    cap = min(MAX_WEIGHT, signal["probe_weight"], 0.001 / vol if vol > 0 else MAX_WEIGHT)
    if cap < MIN_WEIGHT:
        return 0.0, f"20日波动率 {vol:.2%} 使风险预算低于最小调仓幅度。"
    return round(cap, 6), (
        f"基金近5日 {ret5:+.2%}、收于10日均线之上，按5个交易日估算往返成本 "
        f"{cost:.2%}，20日波动率 {vol:.2%}。"
    )


def select_probe(signals, navs, fees, *, closed_sessions, blocked=(), allocated=()):
    excluded = set(blocked)
    occupied = set(allocated)
    candidates = []
    rejected = []
    for s in sorted(signals, key=lambda x: (-x.get("score", 0), str(x.get("board_id", "")))):
        if not s.get("eligible"):
            continue
        funds = s.get("funds") or []
        if any(f["code"] in occupied for f in funds):
            rejected.append(
                f"{s.get('board', '')} 已有持仓或核心仓位计划，避免重复承担同板块风险。"
            )
            continue
        if not funds:
            rejected.append(f"{s.get('board', '')}缺少已核验的场外基金映射。")
        for fund in funds:
            code = fund["code"]
            if code in excluded:
                rejected.append(f"{code} 已持有、已有核心目标或处于冷却期。")
                continue
            weight, reason = probe_weight(
                s, navs.get(code), closed_sessions=closed_sessions, round_trip_fee=fees.get(code)
            )
            if weight:
                candidates.append((s, fund, weight, reason))
            else:
                rejected.append(f"{code} {reason}")
    if candidates:
        return min(
            candidates,
            key=lambda v: (
                -v[0]["score"],
                fees[v[1]["code"]],
                v[1]["code"],
                str(v[0].get("board_id", "")),
            ),
        ), rejected
    return None, rejected


def apply_probe(
    targets,
    held,
    signals,
    navs,
    fees,
    *,
    closed_sessions,
    day,
    previous=None,
    cash_weight=0.0,
    allow_new=True,
    force_exit=False,
):
    today = _day(day)
    dates = sorted({_day(d) for d in closed_sessions if _day(d) < today})
    latest_signal = dates[-1].isoformat() if dates else None
    signals = [
        s for s in signals if latest_signal is not None and s.get("signal_date") == latest_signal
    ]
    base = dict(targets) if targets is not None else dict(held)
    previous = previous or {}
    # Source is the submitted plan; only actual holdings establish an entry.
    code = previous.get("code")
    was_active = previous.get("action") in ("hold", "exit") or (
        previous.get("action") == "enter" and previous.get("instruction_submitted") is not False
    )
    cooldowns = dict(previous.get("cooldowns") or {})
    if previous.get("last_exit_code") and previous.get("last_exit_day"):
        cooldowns[previous["last_exit_code"]] = previous["last_exit_day"]
    if was_active and previous["action"] == "exit" and code and not held.get(code):
        cooldowns[code] = previous.get("decision_day", today.isoformat())
    cooldowns = {
        c: when for c, when in cooldowns.items() if sum(d >= _day(when) for d in dates) <= HORIZON
    }
    latest = max(cooldowns, key=cooldowns.get, default=None)
    plan = {
        "action": "idle",
        "state": None,
        "code": None,
        "name": None,
        "board_id": None,
        "board": None,
        "support": None,
        "entry_day": None,
        "decision_day": today.isoformat(),
        "last_exit_code": latest,
        "last_exit_day": cooldowns.get(latest),
        "cooldowns": cooldowns,
        "rejections": [],
        "reason": "未出现具备基金映射、净值与成本确认的试探机会。",
    }
    if was_active and held.get(code, 0) > 0:
        plan.update(
            {
                k: previous.get(k)
                for k in ("state", "code", "name", "board", "board_id", "support", "entry_day")
            }
        )
        signal = next((s for s in signals if s.get("board_id") == plan["board_id"]), {})
        state = signal.get("state")
        close = (signal.get("metrics") or {}).get("close")
        entry = _day(plan["entry_day"])
        timeout = sum(d >= entry for d in dates) >= HORIZON
        support_lost = close is not None and plan["support"] is not None and close < plan["support"]
        reason = None
        if force_exit:
            reason = "组合风控要求退出。"
        elif previous["action"] == "exit":
            reason = "上一轮赎回尚有余份，继续申请退出。"
        elif support_lost:
            reason = f"板块收盘 {close:.2f} 跌破建仓时冻结的支撑 {plan['support']:.2f}。"
        elif state in ("distribution", "breakdown"):
            reason = "出现放量回落或破位，试探假设失效。"
        elif timeout:
            reason = "已到5交易日验证期，申请赎回。"
        if reason:
            base.pop(code, None)
            plan.update(action="exit", reason=reason)
        else:
            base[code] = min(held[code], max(0, 1 - sum(v for c, v in base.items() if c != code)))
            plan.update(action="hold", reason="尚在5交易日验证期，维持已确认仓位，不加码。")
            if base[code] < held[code] - 1e-8:
                plan["reason"] = f"尚在5日验证期，组合仓位上限将试探权重压至 {base[code]:.2%}。"
        return base, plan
    if targets is None or not allow_new or force_exit:
        plan["reason"] = "本轮无新增仓位授权，维持核心指令。"
        return (base if targets is not None else None), plan
    headroom = min(max(0, cash_weight), max(0, 1 - sum(base.values())))
    if headroom + 1e-10 < MIN_WEIGHT:
        plan["reason"] = "可用现金或组合剩余权重低于2%，跳过试探。"
        return base, plan
    allocated = {c for book in (base, held) for c, w in book.items() if w > 0}
    candidate, rejected = select_probe(
        signals, navs, fees, closed_sessions=dates, blocked=cooldowns, allocated=allocated
    )
    plan["rejections"] = rejected
    if not candidate:
        if rejected:
            plan["reason"] = "；".join(rejected[:3])
        return base, plan
    s, f, weight, reason = candidate
    weight = min(headroom, weight)
    base[f["code"]] = weight
    plan.update(
        action="enter",
        state=s["state"],
        code=f["code"],
        name=f.get("name"),
        board=s.get("board"),
        board_id=s["board_id"],
        support=s["support"],
        entry_day=today.isoformat(),
        signal_date=s["signal_date"],
        weight=weight,
        reason=f"{s['evidence'][0]}{reason}",
    )
    return base, plan


def _split_budget(initial_capital, total_month_turnover, month_probe_buys):
    spent = _number(month_probe_buys)
    gross = _number(total_month_turnover)
    if spent is None or gross is None or spent < 0 or gross < 0 or gross < spent - 0.01:
        raise ValueError("独立试探额度需要实际当月双边换手金额及已成交试探申购金额。")
    total = initial_capital * config.load_settings().agent.max_monthly_turnover
    limit = min(total, initial_capital * MAX_WEIGHT)
    return max(0.0, total - limit - (gross - spent)), max(0.0, limit - spent)


def executable_core_targets(core, held, initial_capital, total_month_turnover, month_probe_buys):
    """Exclude unfundable core buys before allocating the independent trial budget."""
    ordinary, _ = _split_budget(initial_capital, total_month_turnover, month_probe_buys)
    if ordinary > 0:
        return dict(core)
    return {code: min(weight, held[code]) for code, weight in core.items() if held.get(code, 0) > 0}


def prepare_orders(
    account,
    targets,
    prices,
    budget,
    plan,
    *,
    month_probe_buys=0.0,
    redeemable=None,
    cash_receivable=0.0,
    isolated_reserve=False,
    total_month_turnover=None,
):
    """Price both trial-budget policies with the same cash, settlement and sale rules."""
    available = max(0.0, budget)
    reserve_limit = account.initial_capital * MAX_WEIGHT
    spent = max(0.0, month_probe_buys)
    if isolated_reserve:
        ordinary_budget, reserve = _split_budget(
            account.initial_capital, total_month_turnover, month_probe_buys
        )
    else:
        reserve = min(available, max(0.0, reserve_limit - spent))
        ordinary_budget = available - reserve
    kwargs = dict(redeemable=redeemable, cash_receivable=cash_receivable)
    ordinary = dict(targets)
    code = plan.get("code")
    entering = plan.get("action") == "enter" and code in ordinary
    if entering and account.shares_of(code) > 0:
        ordinary[code] = min(ordinary[code], account.weights(prices)[code])
    new = entering and not account.shares_of(code)
    if new:
        weight = ordinary.pop(code)
    orders, notes = execution.prepare_orders(account, ordinary, prices, ordinary_budget, **kwargs)
    if not new:
        if reserve and any(ordinary.get(c, 0) > 0 for c in ordinary):
            notes.append(f"月度原换手额内为试探申购保留 {reserve:,.2f} 元。")
        return orders, notes
    # Missing prices freeze the entire instruction, including the trial.
    if any((_number(prices.get(c)) or 0) <= 0 for c in set(targets) | set(account.codes())):
        return execution.prepare_orders(account, targets, prices, 0, **kwargs)
    sold = sum(o["amount"] for o in orders if o["side"] == "sell")
    trial_budget = reserve if isolated_reserve else min(reserve, max(0, available - sold))
    unchanged = account.weights(prices)
    trial, _ = execution.prepare_orders(
        account, {**unchanged, code: weight}, prices, trial_budget, **kwargs
    )
    buy = next((o for o in trial if o["code"] == code and o["side"] == "buy"), None)
    if not buy:
        return orders, [*notes, f"{code} 试探未达到实际可用现金、预留额度或最小成交金额要求。"]
    # Core sales do not finance today's purchases. Apply the exact cash amount
    # once, as well as the protected monthly budget, before freezing other buys.
    orders, notes = execution.prepare_orders(
        account,
        ordinary,
        prices,
        ordinary_budget,
        redeemable=redeemable,
        cash_receivable=cash_receivable + buy["amount"],
    )
    reserved = f"，含 {cash_receivable + buy['amount']:,.2f} 元在途赎回款"
    explanation = f"，可用现金已为本次试探冻结 {buy['amount']:,.2f} 元"
    if cash_receivable:
        explanation += f"，另有 {cash_receivable:,.2f} 元在途赎回款"
    notes = [n.replace(reserved, explanation) for n in notes]
    orders.append(dict(buy, opportunity_probe=True))
    notes.append(f"{code} 从当月试探预留的 {reserve:,.2f} 元中申购 {buy['amount']:,.2f} 元。")
    if isolated_reserve and sum(o["amount"] for o in orders) > available + 0.01:
        notes.append("赎回不受买入限额约束，当月风险赎回与本次独立试探合计将超过原双边换手预算。")
    return orders, notes
