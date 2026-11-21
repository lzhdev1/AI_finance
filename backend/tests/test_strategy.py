from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.strategies import DualMomentum


def _nav(series: dict[str, np.ndarray]) -> pd.DataFrame:
    index = pd.bdate_range("2020-01-01", periods=len(next(iter(series.values()))))
    return pd.DataFrame(series, index=index)


def test_selects_strongest_asset():
    nav = _nav(
        {
            "A": np.linspace(1.0, 2.0, 200),
            "B": np.linspace(1.0, 1.2, 200),
            "D": np.linspace(1.0, 1.02, 200),
        }
    )
    strategy = DualMomentum(defensive="D", lookback=60, top_n=1)
    weights = strategy.target_weights(nav)
    assert weights["A"] == 1.0
    assert weights["B"] == 0.0


def test_top_n_equal_weight():
    nav = _nav(
        {
            "A": np.linspace(1.0, 2.0, 200),
            "B": np.linspace(1.0, 1.2, 200),
            "D": np.linspace(1.0, 1.02, 200),
        }
    )
    weights = DualMomentum(defensive="D", lookback=60, top_n=2).target_weights(nav)
    assert weights["A"] == 0.5
    assert weights["B"] == 0.5


def test_falls_back_to_defensive_when_all_decline():
    nav = _nav(
        {
            "A": np.linspace(1.5, 1.0, 200),
            "B": np.linspace(1.3, 1.0, 200),
            "D": np.linspace(1.0, 1.001, 200),
        }
    )
    weights = DualMomentum(defensive="D", lookback=60, top_n=2).target_weights(nav)
    assert weights["D"] == 1.0
    assert weights["A"] == 0.0


def test_returns_all_codes():
    nav = _nav(
        {
            "A": np.linspace(1.0, 2.0, 200),
            "B": np.linspace(1.0, 1.2, 200),
            "D": np.linspace(1.0, 1.02, 200),
        }
    )
    weights = DualMomentum(defensive="D", lookback=60, top_n=1).target_weights(nav)
    assert set(weights) == {"A", "B", "D"}
    assert sum(weights.values()) == 1.0