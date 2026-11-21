from __future__ import annotations

from datetime import date

import pytest

from quantlab.pods import PodAccount, PodRules, board_overlap, clamp_targets

FEES = {
    "A": {"buy": 0.0, "sell": [[7, 0.015], [999999, 0.0]]},
}


def _account(cash: float = 100_000.0) -> PodAccount:
    return PodAccount(initial_capital=100_000.0, cash=cash, fees=FEES)


def test_buy_deducts_fee_outer():
    acc = _account()
    fill = acc.buy(date(2026, 1, 5), "A", 10_000, 2.0)
    assert fill is not None
    assert fill.fee == 0.0
    assert fill.shares == pytest.approx(5_000)
    assert acc.cash == pytest.approx(90_000)


def test_sell_within_seven_days_charges_penalty():
    acc = _account()
    acc.buy(date(2026, 1, 5), "A", 10_000, 2.0)
    fill = acc.sell(date(2026, 1, 8), "A", 5_000, 2.0)
    assert fill is not None
    assert fill.fee == pytest.approx(10_000 * 0.015)
    assert acc.cash == pytest.approx(90_000 + 10_000 - 150)


def test_sell_after_seven_days_is_free():
    acc = _account()
    acc.buy(date(2026, 1, 5), "A", 10_000, 2.0)
    fill = acc.sell(date(2026, 1, 12), "A", 5_000, 2.0)
    assert fill is not None
    assert fill.fee == 0.0


def test_fifo_splits_lots_by_holding_period():
    acc = _account()
    acc.buy(date(2026, 1, 5), "A", 10_000, 2.0)  # 5000 份
    acc.buy(date(2026, 2, 5), "A", 10_000, 2.0)  # 再 5000 份
    fill = acc.sell(date(2026, 2, 10), "A", 5_000, 2.0)
    assert fill is not None
    # 只卖掉第一批（持有 36 天），第二批不动
    assert fill.fee == 0.0
    assert acc.shares_of("A") == pytest.approx(5_000)


def test_rebalance_skips_small_differences():
    acc = _account()
    acc.buy(date(2026, 1, 5), "A", 50_000, 1.0)
    acc.buy(date(2026, 1, 5), "B", 50_000, 1.0)
    fills = acc.rebalance(date(2026, 2, 20), {"A": 0.5005, "B": 0.4995}, {"A": 1.0, "B": 1.0})
    # 两边的差额都只有 50 元，小于最小下单额 200 元，不应产生订单
    assert fills == []


def test_rebalance_moves_toward_targets():
    acc = _account()
    acc.buy(date(2026, 1, 5), "A", 100_000, 1.0)
    fills = acc.rebalance(date(2026, 2, 20), {"A": 0.5, "B": 0.5}, {"A": 1.0, "B": 1.0})
    weights = acc.weights({"A": 1.0, "B": 1.0})
    assert weights["A"] == pytest.approx(0.5, abs=0.01)
    assert weights["B"] == pytest.approx(0.5, abs=0.01)
    assert len(fills) == 2


def test_clamp_targets_caps_single_position():
    weights = clamp_targets({"A": 0.7, "B": 0.2, "C": 0.1}, max_position=0.35)
    # 注水式截断：A 先被压到 0.35，余下的 0.65 里 B 又超限，再压到 0.35，C 拿剩下的 0.30
    assert weights["A"] == pytest.approx(0.35)
    assert weights["B"] == pytest.approx(0.35)
    assert weights["C"] == pytest.approx(0.30)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_clamp_targets_leaves_cash_when_cap_infeasible():
    """2 只基金各 50% 但上限 35%：不报错、不放宽，而是各压到 35%，留 30% 现金。"""
    weights = clamp_targets({"A": 0.5, "B": 0.5}, max_position=0.35)
    assert weights["A"] == pytest.approx(0.35)
    assert weights["B"] == pytest.approx(0.35)
    assert sum(weights.values()) == pytest.approx(0.70)


def test_clamp_targets_exempts_defensive_asset():
    """防御资产不受集中度上限约束，否则 100% 持短债的防御仓无法成立。"""
    weights = clamp_targets({"BOND": 1.0}, max_position=0.35, exempt={"BOND"})
    assert weights == {"BOND": 1.0}


def test_clamp_targets_exempt_keeps_room_for_risk_assets():
    weights = clamp_targets({"BOND": 0.4, "A": 0.3, "B": 0.3}, max_position=0.35, exempt={"BOND"})
    assert weights["BOND"] == pytest.approx(0.4)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_clamp_targets_exposure_leaves_cash():
    """风险偏好下调时按比例减仓，剩余留现金（而不是把权重重新归一化回满仓）。"""
    weights = clamp_targets({"A": 0.5, "B": 0.5}, max_position=None, exposure=0.5)
    assert sum(weights.values()) == pytest.approx(0.5)
    assert weights["A"] == pytest.approx(0.25)

    capped = clamp_targets({"A": 0.5, "B": 0.5}, max_position=0.35, exposure=0.6)
    assert sum(capped.values()) == pytest.approx(0.6)


def test_missing_fee_schedule_is_not_free():
    """没有费率数据时按监管底线收 1.5%，不能当免费赎回。"""
    from quantlab.costs import holding_rate, sell_fee

    assert holding_rate((), 1) == pytest.approx(0.015)
    assert sell_fee(10000, (), 1) == pytest.approx(150.0)
    assert holding_rate(((7, 0.015), (999999, 0.0)), 30) == pytest.approx(0.0)


def test_hard_stop_rule():
    rules = PodRules(hard_stop_drawdown=0.12)
    assert rules.hard_stop(-0.13)
    assert not rules.hard_stop(-0.05)


def test_retire_needs_min_eval_days():
    rules = PodRules(min_eval_days=20, soft_retire_hit_rate=0.45)
    retire, reason = rules.should_retire(10, cum_return=-0.10, benchmark_return=0.02, hit_rate=0.2)
    assert not retire
    assert "考核期" in reason


def test_retire_when_losing_and_inaccurate():
    rules = PodRules(min_eval_days=20, soft_retire_hit_rate=0.45)
    retire, _ = rules.should_retire(25, cum_return=-0.10, benchmark_return=0.02, hit_rate=0.30)
    assert retire


def test_keep_when_beat_benchmark_even_if_hit_rate_low():
    rules = PodRules(min_eval_days=20, soft_retire_hit_rate=0.45)
    retire, _ = rules.should_retire(25, cum_return=0.15, benchmark_return=0.02, hit_rate=0.20)
    assert not retire


def test_board_overlap():
    assert board_overlap(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert board_overlap(["a"], ["a"]) == pytest.approx(1.0)
    assert board_overlap([], ["a"]) == 0.0