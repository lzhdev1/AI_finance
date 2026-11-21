from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(equity: pd.Series, trades: pd.DataFrame, risk_free: float = 0.0) -> dict[str, float]:
    if equity.empty or len(equity) < 2:
        return {}

    returns = equity.pct_change().dropna()
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = days / 365.25
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((1.0 + total_return) ** (1.0 / years) - 1.0) if total_return > -1 else -1.0
    volatility = float(returns.std(ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0
    sharpe = float((returns.mean() * 252 - risk_free) / volatility) if volatility > 0 else 0.0
    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    calmar = float(cagr / abs(max_drawdown)) if max_drawdown < 0 else 0.0
    fees = float(trades["fee"].sum()) if not trades.empty else 0.0
    turnover = float(trades["amount"].sum() / equity.mean()) if not trades.empty and equity.mean() else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "fees": fees,
        "fee_ratio": fees / float(equity.iloc[0]) if equity.iloc[0] else 0.0,
        "turnover": turnover,
        "days": float(days),
    }


def format_metrics(metrics: dict[str, float]) -> str:
    if not metrics:
        return "无数据"
    return (
        f"累计收益 {metrics['total_return']:.2%} | 年化 {metrics['cagr']:.2%} | "
        f"波动 {metrics['volatility']:.2%} | 夏普 {metrics['sharpe']:.2f} | "
        f"最大回撤 {metrics['max_drawdown']:.2%} | 手续费 {metrics['fees']:.0f} 元 "
        f"({metrics['fee_ratio']:.2%}) | 换手 {metrics['turnover']:.1f}x"
    )