from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .calendar import month_rebalance_dates
from .costs import buy_fee, sell_fee
from .metrics import summarize
from .strategies.base import Strategy

TRADE_COLUMNS = ["date", "code", "side", "amount", "fee", "nav", "shares"]


@dataclass
class Trade:
    date: pd.Timestamp
    code: str
    side: str
    amount: float
    fee: float
    nav: float
    shares: float


@dataclass
class BacktestResult:
    equity: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    nav: pd.DataFrame
    metrics: dict[str, float] = field(default_factory=dict)


def _asset_map(universe: dict) -> dict[str, dict]:
    return {asset["code"]: asset for asset in universe["assets"]}


def _normalize(targets: dict[str, float], codes: list[str]) -> dict[str, float]:
    cleaned = {code: max(float(targets.get(code, 0.0)), 0.0) for code in codes}
    total = sum(cleaned.values())
    if total <= 0:
        return cleaned
    return {code: value / total for code, value in cleaned.items()}


def run_backtest(
    nav: pd.DataFrame,
    strategy: Strategy,
    universe: dict,
    initial_cash: float = 100_000.0,
    rebalance_months: tuple[int, ...] | list[int] = (1, 3, 5, 7, 9, 11),
    min_trade: float = 100.0,
    start: str | None = None,
    end: str | None = None,
) -> BacktestResult:
    """场外基金日频回测。

    关键时点约定：调仓日 T 使用 T-1 及以前的净值生成信号，按 T 日净值成交。
    这样与「14:30 看昨天净值、15:00 前下单、拿当天净值」的真实流程一致。
    """
    nav = nav.dropna(how="all").sort_index()
    if nav.empty:
        raise ValueError("净值数据为空，先运行 scripts/backfill.py")

    start_ts = pd.Timestamp(start) if start else None
    end_ts = pd.Timestamp(end) if end else None
    codes = list(nav.columns)
    assets = _asset_map(universe)
    fees = universe["fees"]
    prices = nav.ffill()
    days = nav.index
    rebalance_days = set(month_rebalance_dates(days, rebalance_months))

    cash = float(initial_cash)
    lots: dict[str, list[list]] = {code: [] for code in codes}  # [shares, buy_date]
    trades: list[Trade] = []
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    weight_rows: list[dict] = []

    def shares_of(code: str) -> float:
        return sum(lot[0] for lot in lots[code])

    def value_of(code: str, day: pd.Timestamp) -> float:
        price = prices.loc[day, code]
        return 0.0 if pd.isna(price) else shares_of(code) * float(price)

    def sell_position(code: str, day: pd.Timestamp, shares: float, price: float) -> None:
        nonlocal cash
        proceeds = shares * price
        fee = 0.0
        remaining = shares
        schedule = fees[assets[code]["share_class"]]["sell"]
        while remaining > 1e-9 and lots[code]:
            lot = lots[code][0]
            take = min(lot[0], remaining)
            fee += sell_fee(take * price, schedule, (day - lot[1]).days)
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots[code].pop(0)
        cash += proceeds - fee
        trades.append(Trade(day, code, "sell", proceeds, fee, price, shares))

    for i, day in enumerate(days):
        # 允许传入更早的数据做动量预热，但只在 start 之后交易和计净值
        if start_ts is not None and day < start_ts:
            continue
        if end_ts is not None and day > end_ts:
            break
        if day in rebalance_days and i > 0:
            as_of = days[i - 1]
            targets = _normalize(strategy.target_weights(nav.loc[:as_of]), codes)
            total = cash + sum(value_of(code, day) for code in codes)
            target_value = {code: total * targets[code] for code in codes}

            # 先卖后买，保证买入时有可用资金
            for code in codes:
                price = nav.loc[day, code]
                if pd.isna(price):
                    continue
                delta = target_value[code] - value_of(code, day)
                if delta >= -min_trade:
                    continue
                shares = min(shares_of(code), (-delta) / float(price))
                if shares > 0:
                    sell_position(code, day, shares, float(price))

            deficits: dict[str, float] = {}
            for code in codes:
                price = nav.loc[day, code]
                if pd.isna(price):
                    continue
                delta = target_value[code] - value_of(code, day)
                if delta > min_trade:
                    deficits[code] = delta

            total_deficit = sum(deficits.values())
            if total_deficit > 0 and cash > 0:
                scale = min(1.0, cash / total_deficit)
                for code, want in deficits.items():
                    price = float(nav.loc[day, code])
                    spend = min(want * scale, cash)
                    if spend <= 1e-9:
                        continue
                    rate = fees[assets[code]["share_class"]]["buy"]
                    fee = buy_fee(spend, rate)
                    shares = (spend - fee) / price
                    cash -= spend
                    lots[code].append([shares, day])
                    trades.append(Trade(day, code, "buy", spend, fee, price, shares))

        total = cash + sum(value_of(code, day) for code in codes)
        equity_rows.append((day, total))
        weight_rows.append(
            {
                "date": day,
                **{code: (value_of(code, day) / total if total > 0 else 0.0) for code in codes},
            }
        )

    equity = pd.Series(dict(equity_rows), name="equity").sort_index()
    weights = pd.DataFrame(weight_rows).set_index("date").sort_index()
    trades_df = (
        pd.DataFrame([trade.__dict__ for trade in trades], columns=TRADE_COLUMNS)
        if trades
        else pd.DataFrame(columns=TRADE_COLUMNS)
    )
    result = BacktestResult(equity=equity, weights=weights, trades=trades_df, nav=nav)
    result.metrics = summarize(equity, trades_df)
    return result