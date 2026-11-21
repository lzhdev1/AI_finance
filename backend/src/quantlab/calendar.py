from __future__ import annotations

import logging

import pandas as pd

from .config import LOG_DIR, Settings

log = logging.getLogger(__name__)


def setup_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_DIR / "quantlab.log", encoding="utf-8"),
        ],
    )


def as_date(value: object) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def trading_days(calendar: pd.DataFrame, start: str | None = None, end: str | None = None) -> pd.DatetimeIndex:
    days = pd.DatetimeIndex(pd.to_datetime(calendar["trade_date"])).normalize().sort_values()
    if start:
        days = days[days >= pd.Timestamp(start)]
    if end:
        days = days[days <= pd.Timestamp(end)]
    return days


def month_rebalance_dates(days: pd.DatetimeIndex, months: tuple[int, ...] | list[int]) -> pd.DatetimeIndex:
    """每个目标月份的第一个交易日。"""
    months = set(months)
    result = []
    previous_month = None
    for day in days:
        if day.month in months and day.month != previous_month:
            result.append(day)
        previous_month = day.month
    return pd.DatetimeIndex(result)


def is_rebalance_day(
    days: pd.DatetimeIndex, day: pd.Timestamp, months: tuple[int, ...] | list[int]
) -> bool:
    if day not in days or day.month not in set(months):
        return False
    idx = days.get_loc(day)
    return idx == 0 or days[idx - 1].month != day.month


def signal_cutoff_note(settings: Settings) -> str:
    return f"场外基金模拟指令在 {settings.timezone} 15:00 截止，按当日公布净值确认"
