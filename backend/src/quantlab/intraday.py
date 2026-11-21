"""盘中板块指标：把快照算成能判断的数。

口径（都写在页面上，避免"看起来专业但其实算错了"）：

* **量比** = 当日累计成交额 ÷ (过去 5 日日均成交额 × 已交易时间占比)。
  免费源拿不到历史分时，只能用日线均额按时间折算——开盘一分钟量比天然很高，
  所以必须先做时间归一。
* **RS** = 板块涨幅 − 沪深300 涨幅。回答"是真强，还是大盘普涨带的"。
* **广度** = 上涨家数 ÷ (上涨 + 下跌)。回答"是普涨，还是两三只票拉指数"。
* **资金强度** = 主力净占比（%）。东财口径，逐笔大单估算，不是真实席位数据。
* **30 分钟涨速** = 当前涨幅 − 上一档快照涨幅。
* **确认次数** = 当天进入涨幅前 20 名的快照次数。每小时采样最大的价值就在这里：
  只看收盘一个时点，分不清"真强"和"刚好这一分钟强"。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from quantlab import db

TOP_N = 20


def session_progress(ts: pd.Timestamp) -> float:
    """已交易时间占全天 240 分钟的比值。收盘后固定为 1。"""
    minutes = ts.hour * 60 + ts.minute
    open_am, close_am, open_pm = 9 * 60 + 30, 11 * 60 + 30, 13 * 60
    if minutes <= open_am:
        elapsed = 1
    elif minutes <= close_am:
        elapsed = minutes - open_am
    elif minutes <= open_pm:
        elapsed = 120
    else:
        elapsed = 120 + min(minutes - open_pm, 120)
    return max(elapsed, 1) / 240


def _snapshots(day: dt.date) -> pd.DataFrame:
    frame = db.query_df(
        f"""
        SELECT ts, board_name, pct_chg, kind, amount, volume, turnover_rate,
               main_net, main_ratio, up_count, down_count
        FROM {db.qualified('board_intraday')}
        WHERE ts::date = %s
        ORDER BY board_name, ts
        """,
        [day],
    )
    if not frame.empty:
        frame["ts"] = pd.to_datetime(frame["ts"])
    return frame


def _avg_amount(day: dt.date, days: int = 5) -> pd.Series:
    frame = db.query_df(
        f"""
        SELECT board_name, avg(amount) AS avg_amount FROM (
            SELECT b.name AS board_name, q.amount,
                   row_number() OVER (PARTITION BY b.name ORDER BY q.trade_date DESC) AS n
            FROM {db.qualified('board_quote')} q JOIN {db.qualified('board')} b USING (board_id)
            WHERE q.trade_date < %s AND q.amount > 0
        ) a WHERE n <= %s GROUP BY board_name HAVING count(*) = %s
        """,
        [day, days, days],
    )
    if frame.empty:
        return pd.Series(dtype=float)
    return frame.set_index("board_name")["avg_amount"].astype(float)


def _hs300(day: dt.date) -> float | None:
    row = db.query_one(
        f"""
        SELECT pct_chg FROM {db.qualified('index_intraday')}
        WHERE index_code = '000300' AND ts::date = %s
        ORDER BY ts DESC LIMIT 1
        """,
        [day],
    )
    return float(row["pct_chg"]) if row else None


def board_metrics(day: dt.date | None = None) -> pd.DataFrame:
    """当天最近一档快照 + 派生指标。没有快照就返回空表。"""
    day = day or dt.date.today()
    frame = _snapshots(day)
    if frame.empty:
        return frame

    latest = frame.sort_values("ts").groupby("board_name").tail(1).copy()
    latest["ts"] = pd.to_datetime(latest["ts"])
    progress = session_progress(latest["ts"].max())

    avg5 = _avg_amount(day)
    latest["avg5_amount"] = latest["board_name"].map(avg5)
    expected = latest["avg5_amount"] * progress
    latest["volume_ratio"] = (latest["amount"] / expected).where(expected > 0)

    # 降级到新浪时板块名与历史表对不上，量比算不出来；
    # 用"成交额占全市场板块成交额的比重"顶上——它回答的是"钱更愿意去哪"，不是"比平时放量吗"。
    total_amount = float(latest["amount"].sum()) if latest["amount"].notna().any() else 0.0
    latest["amount_share"] = (
        latest["amount"] / total_amount if total_amount > 0 else pd.Series(pd.NA, index=latest.index)
    )

    up = pd.to_numeric(latest["up_count"], errors="coerce").fillna(0)
    down = pd.to_numeric(latest["down_count"], errors="coerce").fillna(0)
    total = up + down
    latest["breadth"] = (up / total).where(total > 0)

    benchmark = _hs300(day)
    latest["rs"] = latest["pct_chg"] - benchmark if benchmark is not None else None

    # 30 分钟涨速：找当前快照之前最近的一档
    stamps = sorted(frame["ts"].unique())
    if len(stamps) >= 2:
        previous = stamps[-2]
        prev = frame[frame["ts"] == previous].set_index("board_name")["pct_chg"]
        latest["speed_30m"] = latest["pct_chg"] - latest["board_name"].map(prev)
    else:
        latest["speed_30m"] = None

    # 确认次数：当天每一档快照里进入前 20 名几次
    ranks = frame.copy()
    ranks["rank"] = ranks.groupby("ts")["pct_chg"].rank(ascending=False, method="min")
    confirm = (
        ranks[ranks["rank"] <= TOP_N].groupby("board_name").size().rename("confirm_count")
    )
    latest["confirm_count"] = latest["board_name"].map(confirm).fillna(0).astype(int)

    # 综合强度：四项分位数的加权，避免单一指标说了算
    def pct_rank(series: pd.Series) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce")
        if values.notna().sum() < 2:
            return pd.Series(0.5, index=series.index)
        return values.rank(pct=True).fillna(0.5)

    # 量比缺失（降级源对不上名字）时，用成交占比顶替，别让这一项恒为 0.5
    volume_component = (
        latest["volume_ratio"] if latest["volume_ratio"].notna().any() else latest["amount_share"]
    )

    latest["score"] = (
        0.40 * pct_rank(latest["pct_chg"])
        + 0.25 * pct_rank(latest["main_ratio"])
        + 0.20 * pct_rank(volume_component)
        + 0.15 * pct_rank(latest["breadth"])
    )
    latest["score"] = (latest["score"] * 100).round(1)

    return latest.sort_values("score", ascending=False).reset_index(drop=True)


def pool(day: dt.date | None = None, top: int = 15) -> pd.DataFrame:
    frame = board_metrics(day)
    if frame.empty:
        return frame
    return frame.head(top)


def snapshot_times(day: dt.date | None = None) -> list[str]:
    day = day or dt.date.today()
    rows = db.query(
        f"""
        SELECT DISTINCT ts FROM {db.qualified('board_intraday')}
        WHERE ts::date = %s ORDER BY ts
        """,
        [day],
    )
    return [row["ts"].strftime("%H:%M") for row in rows]
