from datetime import date, timedelta

import pandas as pd
import pytest

from quantlab import manager_v2
from quantlab.pods import Lot, PodAccount


def test_forecast_uses_mature_non_overlapping_windows_and_shrinks_direction():
    prices = pd.Series([1.01**i for i in range(101)])
    five = manager_v2.forecast(prices, 5, {5: {"days": 15, "latest": "2025-07-15", "shift": -0.06}})
    assert five["samples"] == five["all_samples"] == 16
    unconditional = (16 + 6) / (16 + 12)
    raw = (16 + 12 * unconditional) / (16 + 12)
    assert five["raw_prob_up"] == round(raw, 6)
    assert five["prob_up"] == round(raw - 0.06, 4)
    assert 0 < five["expected_return"] < 0.0511
    assert manager_v2.forecast(prices.iloc[:55], 5, {}) is None


def test_fees_and_volatility_control_only_eligible_increments():
    near_cost = {
        "expected_return": 0.009,
        "prob_up": 0.55,
        "round_trip_fee": 0.01,
        "daily_volatility": 0.03,
    }
    features = {
        "new": near_cost,
        "held": near_cost,
        "liquid": {**near_cost, "expected_return": 0.025},
    }
    weights, notes = manager_v2.entry_budget(
        {"new": 0.1, "held": 0.2, "liquid": 0.35}, features, {"held": 0.1}
    )
    assert weights == {"held": 0.1, "liquid": 0.2}
    assert len(notes) == 3
    assert manager_v2.entry_budget(None, {}, {}) == (None, [])


def test_fifo_delay_preserves_cash_budget_and_hard_stop_exits_immediately():
    day = date(2025, 6, 20)
    acct = PodAccount(
        10000,
        6000,
        {"old": [Lot(1500, day - timedelta(days=20)), Lot(2500, day - timedelta(days=2))]},
        fees={"old": {"sell": [[7, 0.015], [999, 0]]}},
    )
    prices = {"old": 1.0, "new": 1.0}
    # The previous session already wanted the new fund and had cut the old target.
    weights, notes = manager_v2.execution_targets(
        acct, prices, day, {"new": 0.35}, {"new": 0.35}, {}
    )
    assert weights == pytest.approx({"old": 0.25, "new": 0.10})
    assert any("FIFO" in n for n in notes)
    # An initial ordinary cut is deferred. Urgent downside larger than the fee gap can exit.
    weights, _ = manager_v2.execution_targets(acct, prices, day, {}, {"old": 0.4}, {})
    assert weights == {"old": 0.4}
    weights, _ = manager_v2.execution_targets(
        acct,
        prices,
        day,
        {},
        {"old": 0.4},
        {"old": {"expected_return": -0.03, "negative_trend": True}},
    )
    assert weights == {}
    weights, _ = manager_v2.execution_targets(
        acct, prices, day, {"new": 0.35}, None, {}, hard_stop=True
    )
    assert weights == {}
