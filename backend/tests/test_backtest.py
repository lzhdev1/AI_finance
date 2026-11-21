from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.backtest import run_backtest
from quantlab.calendar import month_rebalance_dates
from quantlab.strategies.base import Strategy

UNIVERSE = {
    "defensive": "D",
    "rebalance_months": [1, 3, 5, 7, 9, 11],
    "fees": {
        "A": {"buy": 0.0012, "sell": [[7, 0.015], [365, 0.005], [999999, 0.0]]},
        "C": {"buy": 0.0, "sell": [[7, 0.015], [999999, 0.0]]},
    },
    "assets": [
        {"code": "A", "name": "A", "role": "risk", "share_class": "A"},
        {"code": "B", "name": "B", "role": "risk", "share_class": "A"},
        {"code": "D", "name": "D", "role": "defensive", "share_class": "A"},
    ],
}


def _nav(periods: int = 500) -> pd.DataFrame:
    index = pd.bdate_range("2019-01-01", periods=periods)
    return pd.DataFrame(
        {
            "A": 1.0 * np.power(1.001, np.arange(periods)),
            "B": 1.0 * np.power(1.0002, np.arange(periods)),
            "D": 1.0 * np.power(1.00005, np.arange(periods)),
        },
        index=index,
    )


class RecordingStrategy(Strategy):
    name = "recording"

    def __init__(self) -> None:
        self.seen: list[pd.Timestamp] = []

    def target_weights(self, nav: pd.DataFrame) -> dict[str, float]:
        self.seen.append(nav.index[-1])
        return {"A": 1.0}


def test_backtest_grows_and_trades():
    result = run_backtest(_nav(), RecordingStrategy(), UNIVERSE, initial_cash=100_000)
    assert not result.trades.empty
    assert result.equity.iloc[-1] > 100_000
    assert result.metrics["turnover"] > 0
    assert result.trades["fee"].sum() > 0


def test_weights_are_non_negative_and_bounded():
    result = run_backtest(_nav(), RecordingStrategy(), UNIVERSE, initial_cash=100_000)
    weights = result.weights
    assert (weights >= -1e-9).all().all()
    assert (weights.sum(axis=1) <= 1.0 + 1e-9).all()


def test_no_lookahead():
    """每个调仓日只能看到前一交易日的净值。"""
    nav = _nav()
    strategy = RecordingStrategy()
    start = nav.index[80]
    run_backtest(nav, strategy, UNIVERSE, initial_cash=100_000, start=start)

    expected = [day for day in month_rebalance_dates(nav.index, UNIVERSE["rebalance_months"]) if day >= start]
    assert len(strategy.seen) == len(expected)
    for trade_day, seen in zip(expected, strategy.seen):
        index = nav.index.get_loc(trade_day)
        assert seen < trade_day
        assert seen == nav.index[index - 1]