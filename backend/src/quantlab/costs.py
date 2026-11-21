from __future__ import annotations

from collections.abc import Sequence

# 费率表格式：[持有天数上限, 费率]，持有天数 < 上限时适用
Schedule = Sequence[Sequence[float]]

# 场外基金的监管底线：持有少于 7 天，赎回费不低于 1.5% 且全额计入基金资产。
# 数据缺档时用它兜底——宁可高估成本，也不要假装免费赎回。
DEFAULT_SELL_SCHEDULE: Schedule = ((7, 0.015), (999_999, 0.0))


def holding_rate(schedule: Schedule, holding_days: float) -> float:
    if not schedule:
        # 没有费率数据不等于免费：按监管底线收 1.5%
        return 0.015
    for limit, rate in schedule:
        if holding_days < float(limit):
            return float(rate)
    return float(schedule[-1][1])


def buy_fee(amount: float, rate: float) -> float:
    """外扣法：申购金额中包含手续费。"""
    if amount <= 0 or rate <= 0:
        return 0.0
    return amount - amount / (1.0 + rate)


def sell_fee(amount: float, schedule: Schedule, holding_days: float) -> float:
    if amount <= 0:
        return 0.0
    return amount * holding_rate(schedule, holding_days)