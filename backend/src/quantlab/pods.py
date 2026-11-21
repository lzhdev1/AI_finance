"""模拟仓（pod）的账本与生命周期规则。

两件事分开：
* ``PodAccount``  —— 纯内存账本，只做「给定净值序列，按目标权重调仓」的算术，
  与 backtest.py 的约定完全一致（决策用 T-1 数据，成交按 T 日净值，FIFO 算赎回费）。
* ``rules``       —— 生命周期判定，全部是阈值比较，不做任何"感觉"判断。

这样任何一天的动作都可以在测试里重放，不依赖数据库和网络。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from .costs import buy_fee, sell_fee

log = logging.getLogger(__name__)

# 费率表格式：[持有天数上限, 费率]，持有天数 < 上限时适用
FeeBook = Mapping[str, Mapping[str, object]]  # {code: {"buy": 0.0, "sell": [[7, 0.015], ...]}}


@dataclass
class Lot:
    shares: float
    buy_date: date
    unit_cost: float = 0.0


@dataclass
class Fill:
    """成交记录（一笔订单）。"""

    date: date
    code: str
    side: str
    amount: float
    shares: float
    nav: float
    fee: float
    reason: str = ""


@dataclass
class PodAccount:
    """一个仓的持仓与现金。市值一律用外部传入的当日净值计算。"""

    initial_capital: float
    cash: float
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    fees: dict[str, dict[str, object]] = field(default_factory=dict)
    min_trade_amount: float = 200.0

    # ------------------------------------------------------------ 读

    def codes(self) -> list[str]:
        return list(self.lots)

    def shares_of(self, code: str) -> float:
        return sum(lot.shares for lot in self.lots.get(code, []))

    def value_of(self, code: str, price: float | None) -> float:
        shares = self.shares_of(code)
        if not shares:
            return 0.0
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError(f"持仓 {code} 缺少有效净值")
        return shares * float(price)

    def average_cost(self, code: str) -> float:
        shares = self.shares_of(code)
        return sum(lot.shares * lot.unit_cost for lot in self.lots.get(code, [])) / shares if shares else 0.0

    def market_value(self, prices: Mapping[str, float]) -> float:
        return sum(self.value_of(code, prices.get(code)) for code in self.lots)

    def total_value(self, prices: Mapping[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def weights(self, prices: Mapping[str, float]) -> dict[str, float]:
        total = self.total_value(prices)
        if total <= 0:
            return {code: 0.0 for code in self.lots}
        return {code: self.value_of(code, prices.get(code)) / total for code in self.lots}

    def holding_days(self, code: str, day: date) -> int:
        """最早一批的持有天数，用于提示赎回费。"""
        lots = self.lots.get(code) or []
        return max((day - lot.buy_date).days for lot in lots) if lots else 0

    # ------------------------------------------------------------ 写

    def buy(self, day: date, code: str, amount: float, price: float, reason: str = "") -> Fill | None:
        """申购。amount 为扣款金额，手续费外扣。"""
        if not math.isfinite(amount) or not math.isfinite(price) or price <= 0:
            return None
        amount = round(amount, 2)
        if amount <= 0 or amount > self.cash + 1e-8:
            return None
        rate = float(self.fees.get(code, {}).get("buy", 0.0) or 0.0)
        fee = round(buy_fee(amount, rate), 4)
        shares = round((amount - fee) / price, 6)
        if shares <= 0:
            return None
        fill = Fill(day, code, "buy", amount, shares, price, fee, reason)
        self.apply_fill(fill)
        return fill

    def apply_fill(self, fill: Fill) -> None:
        """从已确认的金额、份额与费用还原历史账本，不按今天的费率重算。"""
        if fill.side == "buy":
            self.cash = round(self.cash - fill.amount, 4)
            self.lots.setdefault(fill.code, []).append(Lot(
                shares=fill.shares, buy_date=fill.date, unit_cost=fill.amount / fill.shares,
            ))
            return
        if fill.side != "sell":
            raise ValueError(f"未知交易方向 {fill.side}")
        lots = self.lots.get(fill.code, [])
        remaining = fill.shares
        while remaining > 1e-8 and lots:
            take = min(lots[0].shares, remaining)
            lots[0].shares = round(lots[0].shares - take, 6)
            remaining -= take
            if lots[0].shares <= 1e-8:
                lots.pop(0)
        if remaining > 1e-5:
            raise ValueError(f"赎回 {fill.code} 超过账面份额")
        if not lots:
            self.lots.pop(fill.code, None)
        self.cash = round(self.cash + fill.amount - fill.fee, 4)

    def sell(self, day: date, code: str, shares: float, price: float, reason: str = "") -> Fill | None:
        """赎回。按 FIFO 批次计算持有天数与赎回费。"""
        if not math.isfinite(shares) or not math.isfinite(price):
            return None
        shares = round(min(shares, self.shares_of(code)), 6)
        if shares <= 0 or price <= 0:
            return None
        proceeds = round(shares * price, 2)
        schedule = (self.fees.get(code, {}).get("sell") or []) if self.fees else []
        fee = 0.0
        remaining = shares
        lots = self.lots.get(code, [])
        for lot in lots:
            take = min(lot.shares, remaining)
            fee += sell_fee(take * price, schedule, (day - lot.buy_date).days)
            remaining -= take
            if remaining <= 1e-9:
                break
        fill = Fill(day, code, "sell", proceeds, shares, price, round(fee, 4), reason)
        self.apply_fill(fill)
        return fill

    def rebalance(
        self,
        day: date,
        targets: Mapping[str, float],
        prices: Mapping[str, float],
        reason: str = "",
        band: float = 0.0,
        max_trade: float | None = None,
    ) -> list[Fill]:
        """按目标权重调仓：先卖后买，差额小于阈值不动。

        两个"止损于摩擦成本"的机制：
        * ``band``——偏离小于总资产的这个比例就懒得动（无交易带），
          否则每天为了几个基点的漂移反复申赎，手续费会把收益吃光；
        * ``max_trade``——本轮最多允许成交的金额（月度换手预算的剩余额度），
          卖出不受限（降风险永远允许），买入在额度用尽后停止。

        这里**不对权重做任何截断**：权重合规性由 clamp_targets() 在决策层保证。
        账本只负责忠实地执行，否则限仓规则会悄悄改变配置意图（留下未投资的现金）。
        """
        total = self.total_value(prices)
        target_value = {
            code: total * max(float(targets.get(code, 0.0)), 0.0)
            for code in set(list(targets) + list(self.lots))
        }
        floor = max(self.min_trade_amount, band * total)

        fills: list[Fill] = []
        bought = 0.0

        # 先卖：腾出资金
        for code in list(self.lots):
            price = prices.get(code)
            if price is None or price != price:
                continue
            delta = target_value.get(code, 0.0) - self.value_of(code, price)
            # 目标权重为 0 时必须清仓：否则无交易带会留下几十块的"碎股"脏持仓
            if delta > -floor and target_value.get(code, 0.0) > 0:
                continue
            shares = min(self.shares_of(code), (-delta) / float(price))
            fill = self.sell(day, code, shares, float(price), reason)
            if fill:
                fills.append(fill)

        # 后买：只买有缺口的
        deficits = {}
        for code, want in target_value.items():
            price = prices.get(code)
            if price is None or price != price:
                continue
            delta = want - self.value_of(code, price)
            if delta > floor:
                deficits[code] = delta

        if deficits and self.cash > 0:
            total_deficit = sum(deficits.values())
            scale = min(1.0, self.cash / total_deficit)
            for code, want in deficits.items():
                if max_trade is not None and bought >= max_trade:
                    break
                price = float(prices[code])
                spend = min(want * scale, self.cash)
                if max_trade is not None:
                    spend = min(spend, max_trade - bought)
                if spend <= floor:
                    continue
                fill = self.buy(day, code, spend, price, reason)
                if fill:
                    fills.append(fill)
                    bought += fill.amount

        return fills


# ================================================================ 权重合规

def clamp_targets(
    targets: Mapping[str, float],
    max_position: float | None = None,
    exempt: Sequence[str] = (),
    exposure: float = 1.0,
) -> dict[str, float]:
    """归一化目标权重，并按单只上限做"注水式"截断。

    关键点：截断后必须重新分配余量，否则会留下不投资的现金，配置意图被悄悄改写。
    如果「标的数量 × 单只上限」小于要配置的仓位，说明这个上限下根本配不满，
    此时**不放宽上限、也不报错，而是保留现金**——集中度是硬约束，宁可少投。

    ``exempt`` 里的标的（防御资产）不受单只上限约束——防御仓本来就该 100% 押在
    一只短债基金上，对它做集中度限制等于禁止防御仓存在。

    ``exposure`` < 1 表示主动降低总仓位（风险偏好下调），差额留作现金。
    """
    weights = {code: max(float(value), 0.0) for code, value in targets.items() if float(value) > 0}
    total = sum(weights.values())
    if total <= 0:
        return {}
    if exposure <= 0:
        return {}
    weights = {code: value / total * min(exposure, 1.0) for code, value in weights.items()}
    if max_position is None:
        return weights

    exempt_set = set(exempt)
    fixed = {code: value for code, value in weights.items() if code in exempt_set}
    movable = {code: value for code, value in weights.items() if code not in exempt_set}
    if not movable:
        return weights

    budget = max(sum(weights.values()) - sum(fixed.values()), 0.0)
    movable_total = sum(movable.values())
    rest = {code: value * budget / movable_total for code, value in movable.items()}

    if len(rest) * max_position < budget - 1e-9:
        # 配不满：把所有标的压到上限，差额留作现金
        return {**fixed, **{code: max_position for code in rest}}

    capped: dict[str, float] = {}
    for _ in range(len(rest)):
        over = [code for code, value in rest.items() if value > max_position + 1e-12]
        if not over:
            break
        for code in over:
            capped[code] = max_position
            rest.pop(code)
        remaining = budget - sum(capped.values())
        rest_total = sum(rest.values())
        if rest_total <= 0:
            break
        rest = {code: value * remaining / rest_total for code, value in rest.items()}
    return {**fixed, **capped, **rest}


# ================================================================ 生命周期规则

@dataclass(frozen=True)
class PodRules:
    min_eval_days: int = 20
    hard_stop_drawdown: float = 0.12
    soft_retire_hit_rate: float = 0.45
    cooldown_days: int = 10
    max_pods: int = 5

    def hard_stop(self, drawdown: float) -> bool:
        """回撤触及止损线 → 立即停牌，不找理由。"""
        return drawdown <= -abs(self.hard_stop_drawdown)

    def eval_ready(self, days_elapsed: int) -> bool:
        return days_elapsed >= self.min_eval_days

    def should_retire(self, days_elapsed: int, cum_return: float, benchmark_return: float, hit_rate: float | None) -> tuple[bool, str]:
        """到期评估：跑输基准 且 预测命中率不达标 → 退役。"""
        if not self.eval_ready(days_elapsed):
            return False, "未到最短考核期"
        if cum_return > benchmark_return:
            return False, "跑赢基准，继续保留"
        if hit_rate is None:
            return False, "无足够预测样本，继续观察"
        if hit_rate >= self.soft_retire_hit_rate:
            return False, "命中率达标，继续保留"
        return True, (
            f"考核期满：累计 {cum_return:.2%} 跑输基准 {benchmark_return:.2%}，"
            f"预测命中率 {hit_rate:.0%} 低于 {self.soft_retire_hit_rate:.0%}"
        )


def board_overlap(boards_a: Sequence[str], boards_b: Sequence[str], weights: Mapping[str, float] | None = None) -> float:
    """两个仓的板块重合度：交集权重 / 并集权重，0~1。"""
    set_a, set_b = set(boards_a), set(boards_b)
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if weights:
        num = sum(weights.get(b, 1.0) for b in set_a & set_b)
        den = sum(weights.get(b, 1.0) for b in union)
    else:
        num, den = len(set_a & set_b), len(union)
    return num / den if den else 0.0
