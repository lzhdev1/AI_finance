from __future__ import annotations

import argparse

import pandas as pd

from quantlab.backtest import run_backtest
from quantlab.config import load_settings, load_universe, universe_codes
from quantlab.data import load_nav
from quantlab.metrics import format_metrics
from quantlab.signals import build_strategy


def main() -> None:
    parser = argparse.ArgumentParser(description="命令行回测")
    parser.add_argument("--lookback", type=int, default=60)
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--no-absolute", action="store_true")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    settings = load_settings()
    universe = load_universe()
    start = args.start or settings.backtest_start
    end = args.end or settings.backtest_end
    buffer_start = (
        pd.Timestamp(start) - pd.Timedelta(days=max(args.lookback * 2, 120))
    ).strftime("%Y-%m-%d")
    nav = load_nav(universe_codes(universe), start=buffer_start)
    result = run_backtest(
        nav,
        build_strategy(universe, args.lookback, args.top_n, not args.no_absolute),
        universe,
        initial_cash=settings.initial_cash,
        rebalance_months=universe["rebalance_months"],
        start=start,
        end=end,
    )
    print(format_metrics(result.metrics))
    print(f"成交 {len(result.trades)} 笔")
    if not result.trades.empty:
        print(result.trades.to_string(index=False))


if __name__ == "__main__":
    main()