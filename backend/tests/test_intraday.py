"""盘中指标口径测试：量比的时间归一、广度、综合强度都要能被验证。"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from quantlab import intraday


def test_session_progress_boundaries() -> None:
    """量比必须按"已交易时间占比"归一，否则开盘一分钟量比会虚高十倍。"""
    day = "2026-11-02"
    assert abs(intraday.session_progress(pd.Timestamp(f"{day} 09:30")) - 1 / 240) < 1e-9
    assert abs(intraday.session_progress(pd.Timestamp(f"{day} 11:30")) - 0.5) < 1e-9
    # 午休不算交易时间
    assert abs(intraday.session_progress(pd.Timestamp(f"{day} 12:30")) - 0.5) < 1e-9
    assert abs(intraday.session_progress(pd.Timestamp(f"{day} 13:30")) - 150 / 240) < 1e-9
    assert abs(intraday.session_progress(pd.Timestamp(f"{day} 15:00")) - 1.0) < 1e-9
    # 收盘后固定满格，不能因为时间继续走而超过 1
    assert intraday.session_progress(pd.Timestamp(f"{day} 20:00")) == 1.0


def test_pool_empty_on_day_without_snapshots() -> None:
    frame = intraday.pool(dt.date(2000, 1, 1))
    assert frame.empty


def test_pool_metrics_shape() -> None:
    frame = intraday.pool(top=5)
    if frame.empty:  # 周末/未采集时跳过，不制造假失败
        return
    for column in ("volume_ratio", "amount_share", "breadth", "rs", "confirm_count", "score"):
        assert column in frame.columns, f"缺少指标列 {column}"
    assert frame["score"].between(0, 100).all()
    assert (frame["confirm_count"] >= 0).all()
    # 按强度降序
    assert frame["score"].is_monotonic_decreasing