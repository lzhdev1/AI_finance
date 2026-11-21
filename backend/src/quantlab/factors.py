"""因子层：把板块行情 + 资金流折算成可排序的分数。

设计上刻意"少而稳"：
* 只用三个有经济含义的因子——短动量、中期动量、资金流强度；
* 全部做横截面 z-score 后再加权，避免不同板块的量纲差异；
* 权重集中在本文件的 ``WEIGHTS`` 里，回测时只调这一处。

为什么资金流要除以成交额：绝对净流入天然偏向大板块（银行一天成交额顶小板块十倍），
不做归一化的话，"主力资金排行"永远等于"市值排行"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

log = logging.getLogger(__name__)

# 因子权重：短动量 45% + 中期动量 30% + 资金流强度 25%
WEIGHTS = {"momentum_5": 0.45, "momentum_20": 0.30, "flow_ratio": 0.25}

# 新闻热度权重。这是个**先验**，不是拟合出来的结果——它只在新闻覆盖率足够时才启用，
# 并且等预测样本攒够后要用校准数据回检，不能凭感觉调大。
NEWS_WEIGHT = 0.10
NEWS_MIN_COVERAGE = 0.30

MIN_HISTORY = 25  # 少于这么多交易日不参与打分


def returns(prices: pd.DataFrame, window: int) -> pd.Series:
    """末日相对 window 个交易日前的收益率（逐列）。"""
    if len(prices) <= window:
        return pd.Series(dtype=float)
    latest = prices.iloc[-1]
    base = prices.iloc[-1 - window]
    return latest / base - 1.0


def zscore(series: pd.Series) -> pd.Series:
    """横截面标准化；样本不足或标准差为 0 时返回全 0。"""
    clean = series.astype(float)
    if clean.dropna().shape[0] < 2:
        return pd.Series(0.0, index=series.index)
    std = clean.std(ddof=0)
    if not std or pd.isna(std):
        return pd.Series(0.0, index=series.index)
    return (clean - clean.mean()) / std


@dataclass(frozen=True)
class BoardScore:
    """一个板块的打分明细——决策卡里"为什么选它"就直接引用这些数。"""

    momentum_5: float
    momentum_20: float
    flow_ratio: float
    crowding: float
    score: float
    news_count: float = 0.0
    news_z: float = 0.0


def board_scores(
    quotes: pd.DataFrame,
    amounts: pd.DataFrame,
    flows: pd.DataFrame,
    min_history: int = MIN_HISTORY,
    news_counts: pd.Series | None = None,
    news_weight: float = NEWS_WEIGHT,
) -> pd.DataFrame:
    """输入三张同形状宽表（index=日期，columns=板块），输出按分数降序的明细表。

    * quotes  —— 收盘价
    * amounts —— 成交额（元）
    * flows   —— 主力净流入（元）
    * news_counts —— 可选，板块 → 近 N 日新闻条数

    新闻只在覆盖率达标时才进分数：只有两三个板块有新闻却给全市场加权，
    等于拿噪声当信号。
    """
    columns = ["momentum_5", "momentum_20", "flow_ratio", "crowding", "score"]
    if quotes.empty or len(quotes) < min_history:
        log.info("板块样本不足（%s 行），本轮不打分", len(quotes))
        return pd.DataFrame(columns=columns)

    boards = [code for code in quotes.columns if quotes[code].notna().sum() >= min_history]
    if not boards:
        return pd.DataFrame(columns=columns)

    quotes = quotes[boards]
    amounts = amounts.reindex(columns=boards).fillna(0.0) if not amounts.empty else pd.DataFrame(0.0, index=quotes.index, columns=boards)
    flows = flows.reindex(columns=boards).fillna(0.0) if not flows.empty else pd.DataFrame(0.0, index=quotes.index, columns=boards)

    mom5 = returns(quotes, 5).reindex(boards)
    mom20 = returns(quotes, 20).reindex(boards)

    flow_5d = flows.tail(5).sum(min_count=1).reindex(boards)
    amount_60d = amounts.tail(60).mean().replace(0, float("nan"))
    flow_ratio = (flow_5d / amount_60d).astype(float)

    amount_latest = amounts.tail(1).mean().reindex(boards)
    crowding = (amount_latest / amounts.tail(60).mean()).astype(float)

    components = pd.DataFrame(
        {
            "momentum_5": mom5.fillna(0.0),
            "momentum_20": mom20.fillna(0.0),
            "flow_ratio": flow_ratio.fillna(0.0),
            "crowding": crowding.fillna(1.0),
        }
    )
    components["score"] = sum(
        weight * zscore(components[name]) for name, weight in WEIGHTS.items()
    )

    if news_counts is not None and len(news_counts) > 0:
        counts = news_counts.reindex(boards).fillna(0.0).astype(float)
        components["news_count"] = counts
        coverage = float((counts > 0).mean())
        if coverage >= NEWS_MIN_COVERAGE:
            components["news_z"] = zscore(counts)
            components["score"] = components["score"] + news_weight * components["news_z"]
        else:
            # 覆盖率太低：只展示条数，不进分数
            components["news_z"] = 0.0
            log.info("新闻覆盖率 %.0f%% 不足 %.0f%%，本轮不计入打分", coverage * 100, NEWS_MIN_COVERAGE * 100)

    return components.sort_values("score", ascending=False)


def to_board_scores(frame: pd.DataFrame) -> dict[str, BoardScore]:
    return {
        board: BoardScore(
            momentum_5=float(row["momentum_5"]),
            momentum_20=float(row["momentum_20"]),
            flow_ratio=float(row["flow_ratio"]),
            crowding=float(row["crowding"]),
            score=float(row["score"]),
            news_count=float(row["news_count"]) if "news_count" in row and pd.notna(row["news_count"]) else 0.0,
            news_z=float(row["news_z"]) if "news_z" in row and pd.notna(row["news_z"]) else 0.0,
        )
        for board, row in frame.iterrows()
    }


def fund_momentum(nav: pd.DataFrame, window: int = 60) -> pd.Series:
    """基金层动量，作为没有板块映射时的兜底信号（等价于原来的双动量）。"""
    return returns(nav, window)


def pick_boards(scores: pd.DataFrame, top_n: int = 3, min_score: float = 0.0) -> list[str]:
    """取分数最高的 top_n 个板块；只留分数为正的（负分意味着各项都弱于中位数）。"""
    if scores.empty:
        return []
    candidates = scores[scores["score"] > min_score]
    return list(candidates.head(top_n).index)