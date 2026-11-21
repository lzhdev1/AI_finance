from __future__ import annotations

import pytest

from quantlab.costs import buy_fee, holding_rate, sell_fee


def test_holding_rate_boundaries():
    schedule = [[7, 0.015], [365, 0.005], [999999, 0.0]]
    assert holding_rate(schedule, 3) == pytest.approx(0.015)
    assert holding_rate(schedule, 7) == pytest.approx(0.005)
    assert holding_rate(schedule, 364) == pytest.approx(0.005)
    assert holding_rate(schedule, 365) == pytest.approx(0.0)


def test_buy_fee_is_deducted_from_amount():
    assert buy_fee(10_000, 0.0012) == pytest.approx(10_000 - 10_000 / 1.0012)
    assert buy_fee(10_000, 0.0) == 0.0
    assert buy_fee(0, 0.001) == 0.0


def test_sell_fee():
    schedule = [[7, 0.015], [999999, 0.0]]
    assert sell_fee(10_000, schedule, 10) == 0.0
    assert sell_fee(10_000, schedule, 3) == pytest.approx(150.0)