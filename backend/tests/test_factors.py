from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.factors import board_scores, fund_momentum, pick_boards, returns, zscore


def _frames(periods: int = 80) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    index = pd.bdate_range("2026-01-01", periods=periods)
    # 强板块：稳步上涨 + 持续净流入；弱板块：下跌 + 净流出；中性板块：横盘
    strong = np.linspace(1.0, 1.35, periods)
    weak = np.linspace(1.0, 0.85, periods)
    flat = np.ones(periods)
    quotes = pd.DataFrame({"强": strong, "弱": weak, "平": flat}, index=index)
    amounts = pd.DataFrame(1e9, index=index, columns=["强", "弱", "平"])
    flows = pd.DataFrame(
        {
            "强": np.full(periods, 2e8),
            "弱": np.full(periods, -2e8),
            "平": np.zeros(periods),
        },
        index=index,
    )
    return quotes, amounts, flows


def test_strong_board_ranks_first():
    quotes, amounts, flows = _frames()
    scores = board_scores(quotes, amounts, flows)
    assert list(scores.index)[0] == "强"
    assert list(scores.index)[-1] == "弱"
    assert scores.loc["强", "score"] > scores.loc["平", "score"] > scores.loc["弱", "score"]


def test_flow_ratio_is_normalised_by_turnover():
    """同样的净流入，成交额大的板块强度应该更低——否则排行等于市值排行。"""
    periods = 80
    index = pd.bdate_range("2026-01-01", periods=periods)
    quotes = pd.DataFrame({"大": np.linspace(1.0, 1.1, periods), "小": np.linspace(1.0, 1.1, periods)}, index=index)
    amounts = pd.DataFrame({"大": 1e10, "小": 1e9}, index=index)
    flows = pd.DataFrame({"大": 1e8, "小": 1e8}, index=index)
    scores = board_scores(quotes, amounts, flows)
    assert scores.loc["小", "flow_ratio"] > scores.loc["大", "flow_ratio"]


def test_insufficient_history_returns_empty():
    index = pd.bdate_range("2026-01-01", periods=5)
    quotes = pd.DataFrame({"A": np.linspace(1, 1.1, 5)}, index=index)
    assert board_scores(quotes, quotes, quotes).empty


def test_missing_board_data_is_dropped():
    quotes, amounts, flows = _frames()
    quotes["新"] = np.nan  # 只有一天数据的新板块
    quotes.loc[quotes.index[-1], "新"] = 1.0
    scores = board_scores(quotes, amounts, flows)
    assert "新" not in scores.index


def test_pick_boards_filters_negative_scores():
    quotes, amounts, flows = _frames()
    scores = board_scores(quotes, amounts, flows)
    picked = pick_boards(scores, top_n=3)
    assert "弱" not in picked
    assert picked[0] == "强"


def test_returns_and_zscore_basics():
    prices = pd.DataFrame({"A": [1.0, 1.0, 1.0, 1.0, 1.0, 1.2]})
    assert returns(prices, 5)["A"] == pytest.approx(0.2)
    z = zscore(pd.Series([1.0, 2.0, 3.0]))
    assert z.mean() == pytest.approx(0.0)
    assert zscore(pd.Series([1.0, 1.0, 1.0])).abs().max() == pytest.approx(0.0)


def test_fund_momentum_matches_total_return():
    nav = pd.DataFrame({"F": np.linspace(1.0, 2.0, 61)})
    assert fund_momentum(nav, window=60)["F"] == pytest.approx(1.0)


def test_news_heat_lifts_mentioned_board():
    quotes, amounts, flows = _frames()
    news = pd.Series({"强": 12.0, "弱": 0.0, "平": 0.0})
    base = board_scores(quotes, amounts, flows)
    tilted = board_scores(quotes, amounts, flows, news_counts=news)
    assert tilted.loc["强", "score"] > base.loc["强", "score"]
    assert tilted.loc["强", "news_count"] == 12.0
    assert "news_z" in tilted.columns


def test_news_ignored_when_coverage_too_low():
    """8 个板块只有 1 个有新闻（12.5% < 30%），不该让它影响排序。"""
    periods = 80
    index = pd.bdate_range("2026-01-01", periods=periods)
    boards = {f"B{i}": np.linspace(1.0, 1.0 + i / 100, periods) for i in range(8)}
    quotes = pd.DataFrame(boards, index=index)
    amounts = pd.DataFrame(1e9, index=index, columns=list(boards))
    flows = pd.DataFrame(0.0, index=index, columns=list(boards))
    news = pd.Series({"B7": 20.0})
    base = board_scores(quotes, amounts, flows)
    tilted = board_scores(quotes, amounts, flows, news_counts=news)
    assert tilted["score"].round(6).equals(base["score"].round(6))