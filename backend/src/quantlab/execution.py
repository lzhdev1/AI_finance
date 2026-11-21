"""Pre-close OTC fund instructions and confirmation at the published unit NAV."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, pod_store
from .config import load_settings
from .pods import PodAccount


def nav_on(day: date | None, codes, *, visible_by: datetime | None = None) -> dict[str, float]:
    if day is None or not codes:
        return {}
    rows = db.query(
        f"SELECT i.code, n.nav FROM {db.qualified('instrument_nav')} n "
        f"JOIN {db.qualified('instrument')} i USING (instrument_id) "
        "WHERE i.code = ANY(%s) AND n.trade_date = %s AND n.nav > 0 "
        "AND (%s::timestamptz IS NULL OR n.ingested_at <= %s)",
        [list(codes), day, visible_by, visible_by],
    )
    return {r["code"]: float(r["nav"]) for r in rows if math.isfinite(r["nav"])}


def monthly_turnover_left(pod: Mapping, day: date) -> float:
    used = db.query_one(
        f"SELECT COALESCE(sum(amount), 0) AS used FROM {db.qualified('pod_order')} "
        "WHERE pod_id = %s AND execute_on BETWEEN %s AND %s",
        [pod["pod_id"], day.replace(day=1), day],
    )
    budget = float(pod["initial_capital"]) * load_settings().agent.max_monthly_turnover
    return max(0.0, budget - float(used["used"]))


def risk_targets(
    targets: Mapping, instruments: Mapping, boards: Mapping, rules
) -> tuple[dict, list]:
    """Only reduce proposed absolute weights. Residual capital stays in cash."""
    weights = {}
    notes = []
    for code, value in targets.items():
        if isinstance(value, bool):
            raise ValueError(f"{code} 的目标权重无效")
        weight = float(value)
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError(f"{code} 的目标权重无效")
        if weight == 0:
            continue
        info = instruments.get(code)
        if not info or not info["is_active"] or info["kind"] != "otc":
            raise ValueError(f"{code} 不在可申赎的场外基金池")
        weights[code] = weight
    if sum(weights.values()) > 1.000001:
        raise ValueError("目标仓位合计超过 100%")
    exposures: dict[str, dict[str, float]] = {}
    for code in weights:
        if instruments[code]["role"] == "defensive":
            continue
        if weights[code] > rules.max_position:
            weights[code] = rules.max_position
            notes.append(f"{code} 单基金目标下调至 {rules.max_position:.0%}")
        for board, ratio in (boards.get(code) or {"未映射板块": 1.0}).items():
            exposures.setdefault(board, {})[code] = ratio
    for board, group in sorted(exposures.items()):
        gross = sum(weights[code] * ratio for code, ratio in group.items())
        if gross > rules.max_board_exposure:
            for code in group:
                weights[code] *= rules.max_board_exposure / gross
            notes.append(f"{board} 暴露按 {rules.max_board_exposure:.0%} 上限缩减")
    return weights, notes


def limit_targets(targets: Mapping) -> tuple[dict, list]:
    if not isinstance(targets, Mapping):
        raise ValueError("目标仓位必须为基金代码与权重的对应表")
    boards: dict[str, dict[str, float]] = {}
    if targets:
        rows = db.query(
            f"SELECT i.code, b.name, ib.weight FROM {db.qualified('instrument_board')} ib "
            f"JOIN {db.qualified('instrument')} i USING (instrument_id) "
            f"JOIN {db.qualified('board')} b USING (board_id) "
            "WHERE i.code = ANY(%s) AND ib.weight > 0",
            [list(targets)],
        )
        for row in rows:
            boards.setdefault(row["code"], {})[row["name"]] = min(1.0, float(row["weight"]))
    return risk_targets(targets, pod_store.instrument_map(), boards, load_settings().agent)


def prepare_orders(
    account: PodAccount, targets: Mapping, prices: Mapping, budget: float,
    *, redeemable: Mapping | None = None, cash_receivable: float = 0,
) -> tuple[list, list]:
    """Sell frozen shares and buy frozen cash amounts. No same-session recycling."""
    codes = sorted(set(targets) | set(account.codes()))
    missing = [
        code for code in codes if not math.isfinite(prices.get(code, 0)) or prices.get(code, 0) <= 0
    ]
    if missing:
        return [], [f"前一交易日净值缺失，维持持仓，暂停本次下单（{'、'.join(missing)}）"]
    total = account.total_value(prices)
    floor = max(account.min_trade_amount, total * 0.02)
    pending, buys, notes = [], [], []
    for code in codes:
        held = account.shares_of(code)
        price = prices[code]
        target = targets.get(code, 0)
        delta = total * target - held * price
        if delta < 0 and (target == 0 or -delta >= floor):
            shares = min(held, round(-delta / price, 6))
            if redeemable is not None:
                available_shares = round(redeemable.get(code, 0), 6)
                if shares > available_shares:
                    notes.append(f"{code} 拟赎回 {shares:g} 份，其中 {shares-available_shares:g} 份尚未到可赎日；"
                                 f"本次最多可提交 {available_shares:g} 份")
                    shares = available_shares
                if shares <= 0:
                    continue
            pending.append(
                {
                    "code": code,
                    "side": "sell",
                    "shares": shares,
                    "amount": round(shares * price, 2),
                    "est_nav": price,
                    "weight": round(shares * price / total, 6),
                }
            )
        elif delta >= floor and delta > 0:
            buys.append((code, delta))
    requested = sum(delta for _, delta in buys)
    if requested:
        sold = sum(order["amount"] for order in pending)
        available = min(max(0, account.cash - cash_receivable), max(0, budget - sold))
        if available + 0.01 < requested:
            notes.append(
                f"申购最多动用 {available:,.2f} 元，受可用现金和当月换手额度约束"
                + (f"，含 {cash_receivable:,.2f} 元在途赎回款" if cash_receivable else "")
            )
        scale = min(1.0, available / requested)
        for code, delta in buys:
            # Round down to avoid overspending the last cent after proportional allocation.
            amount = math.floor((delta * scale + 1e-10) * 100) / 100
            if amount >= account.min_trade_amount:
                pending.append(
                    {
                        "code": code,
                        "side": "buy",
                        "amount": amount,
                        "est_nav": prices[code],
                        "weight": round(amount / total, 6),
                    }
                )
    return pending, notes


def fill_orders(account: PodAccount, day: date, orders: list, prices: Mapping, reason: str,
                *, redeemable: Mapping | None = None, cash_receivable: float = 0) -> list:
    fills = []
    available = account.cash - cash_receivable
    remaining = dict(redeemable) if redeemable is not None else None
    for order in orders:
        code = order["code"]
        if order["side"] == "buy":
            amount = float(order["amount"])
            if amount > available + 1e-8:
                raise ValueError("冻结申购金额超出指令提交时的可用现金")
            fill = account.buy(day, code, amount, prices[code], reason)
            available -= amount
        elif order["side"] == "sell":
            # Legacy cards contain a frozen estimate and NAV, but no explicit shares.
            shares = order.get("shares")
            if shares is None:
                if not order.get("est_nav"):
                    raise ValueError(f"旧赎回指令 {code} 缺少份额和估算净值")
                shares = min(
                    account.shares_of(code),
                    round(float(order["amount"]) / float(order["est_nav"]), 6),
                )
            if float(shares) > account.shares_of(code) + 1e-6:
                raise ValueError(f"{code} 冻结赎回份额超过持有份额")
            if remaining is not None:
                if float(shares) > remaining.get(code, 0) + 1e-7:
                    raise ValueError(f"{code} 冻结赎回份额超过当日已可赎的份额")
                remaining[code] = remaining.get(code, 0) - float(shares)
            fill = account.sell(day, code, float(shares), prices[code], reason)
        else:
            raise ValueError(f"未知指令方向 {order['side']}")
        if fill is None:
            raise ValueError(f"{code} 冻结指令的金额或份额无效")
        fills.append(fill)
    return fills


def mark_to_market(pod: Mapping, through: date) -> dict:
    """Value the book after close on days without orders. Never cross an unsettled decision."""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    last_close = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    end = min(through, last_close)
    marked = []
    with db.transaction():
        pod_id = pod["pod_id"]
        db.query_one(
            f"SELECT pod_id FROM {db.qualified('pod')} WHERE pod_id=%s FOR UPDATE", [pod_id]
        )
        days = db.query(
            f"""SELECT trade_date FROM {db.qualified("trade_calendar")}
                WHERE trade_date BETWEEN %s AND %s
                  AND trade_date > COALESCE((SELECT max(trade_date) FROM {db.qualified("pod_nav")}
                                             WHERE pod_id=%s), '-infinity'::date)
                  AND trade_date < COALESCE((SELECT min(trade_date) FROM {db.qualified("pod_decision")}
                                             WHERE pod_id=%s AND executed_at IS NULL), 'infinity'::date)
                ORDER BY trade_date""",
            [pod["opened_on"], end, pod_id, pod_id],
        )
        for row in days:
            day = row["trade_date"]
            account = pod_store.load_account(pod, as_of=day)
            prices = nav_on(day, account.codes())
            missing = sorted(set(account.codes()) - set(prices))
            if missing:
                return {"valued": marked, "waiting_date": str(day), "missing_nav": missing}
            pod_store.save_snapshot(pod, day, account, prices)
            marked.append(str(day))
    return {"valued": marked}
