"""数据读取层（PostgreSQL）。

写入在 ingest.py，抓取在 sources.py；这里只负责"把库里的事实读成 DataFrame"。
所有函数返回的索引/列约定与原 DuckDB 版本保持一致，回测与信号模块无需改动。
"""

from __future__ import annotations

import logging

import pandas as pd

from . import db
from .config import load_settings, load_universe, universe_codes

log = logging.getLogger(__name__)


def init_db() -> None:
    db.apply_schema()


def _codes(codes: list[str] | None) -> list[str]:
    return codes if codes is not None else universe_codes(load_universe())


def load_nav(
    codes: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    use_acc_nav: bool = False,
) -> pd.DataFrame:
    """净值宽表：index=日期，columns=基金代码。

    默认用单位净值。部分基金的累计净值存在异常跳变（例如 001868 在 2015-11-13
    单日 +20.7% 但单位净值不变），直接用累计净值会污染动量信号。
    """
    codes = _codes(codes)
    value_col = "COALESCE(n.acc_nav, n.nav)" if use_acc_nav else "n.nav"
    sql = [
        f"SELECT n.trade_date AS date, i.code, {value_col} AS value",
        f"FROM {db.qualified('instrument_nav')} n",
        f"JOIN {db.qualified('instrument')} i USING (instrument_id)",
        "WHERE i.code = ANY(%s)",
    ]
    params: list[object] = [codes]
    if start:
        sql.append("AND n.trade_date >= %s")
        params.append(start)
    if end:
        sql.append("AND n.trade_date <= %s")
        params.append(end)
    frame = db.query_df(" ".join(sql), params)
    if frame.empty:
        return pd.DataFrame(columns=codes)
    wide = frame.pivot(index="date", columns="code", values="value").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide.reindex(columns=codes)


def load_calendar(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    sql = [f"SELECT trade_date AS date FROM {db.qualified('trade_calendar')}"]
    params: list[object] = []
    conditions = []
    if start:
        conditions.append("trade_date >= %s")
        params.append(start)
    if end:
        conditions.append("trade_date <= %s")
        params.append(end)
    if conditions:
        sql.append("WHERE " + " AND ".join(conditions))
    sql.append("ORDER BY trade_date")
    return db.query_df(" ".join(sql), params)


def load_index(code: str | None = None) -> pd.Series:
    code = code or load_settings().benchmark
    frame = db.query_df(
        f"SELECT trade_date AS date, close FROM {db.qualified('index_quote')} "
        "WHERE index_code = %s ORDER BY trade_date",
        [code],
    )
    if frame.empty:
        return pd.Series(dtype=float)
    return pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["date"]))


def latest_nav_date() -> pd.Timestamp | None:
    row = db.query_one(f"SELECT max(trade_date) AS d FROM {db.qualified('instrument_nav')}")
    return pd.Timestamp(row["d"]) if row and row["d"] else None


def quality_report(universe: dict | None = None) -> pd.DataFrame:
    """数据质量：行数、覆盖区间、日收益异常、与交易日历的缺失比例。"""
    universe = universe or load_universe()
    codes = universe_codes(universe)
    sql = f"""
        WITH span AS (
            SELECT i.code,
                   min(n.trade_date) AS first_date,
                   max(n.trade_date) AS last_date,
                   count(*)          AS rows
            FROM {db.qualified('instrument_nav')} n
            JOIN {db.qualified('instrument')} i USING (instrument_id)
            WHERE i.code = ANY(%s)
            GROUP BY i.code
        ),
        cal AS (
            SELECT s.code, count(c.trade_date) AS cal_days
            FROM span s
            JOIN {db.qualified('trade_calendar')} c
              ON c.trade_date BETWEEN s.first_date AND s.last_date
            GROUP BY s.code
        ),
        changes AS (
            SELECT i.code,
                   n.nav / nullif(lag(n.nav) OVER (PARTITION BY n.instrument_id ORDER BY n.trade_date), 0) - 1 AS change
            FROM {db.qualified('instrument_nav')} n
            JOIN {db.qualified('instrument')} i USING (instrument_id)
            WHERE i.code = ANY(%s)
        ),
        ret AS (
            SELECT code, max(abs(change)) AS max_abs_daily_return FROM changes GROUP BY code
        )
        SELECT s.code, s.rows, s.first_date AS first, s.last_date AS last,
               round(1 - s.rows::numeric / GREATEST(c.cal_days, 1), 4) AS missing_ratio,
               round(r.max_abs_daily_return, 4) AS max_abs_daily_return
        FROM span s
        JOIN cal c USING (code)
        LEFT JOIN ret r USING (code)
        ORDER BY s.code
    """
    return db.query_df(sql, [codes, codes])


# ---------------------------------------------------------------- 市场侧读取

def load_board_quotes(
    names: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    column: str = "close",
) -> pd.DataFrame:
    """板块行情宽表：index=日期，columns=板块名称。"""
    sql = [
        f"SELECT q.trade_date AS date, b.name, q.{column} AS value",
        f"FROM {db.qualified('board_quote')} q",
        f"JOIN {db.qualified('board')} b USING (board_id)",
        "WHERE 1 = 1",
    ]
    params: list[object] = []
    if names:
        sql.append("AND b.name = ANY(%s)")
        params.append(names)
    if start:
        sql.append("AND q.trade_date >= %s")
        params.append(start)
    if end:
        sql.append("AND q.trade_date <= %s")
        params.append(end)
    frame = db.query_df(" ".join(sql), params)
    if frame.empty:
        return pd.DataFrame()
    wide = frame.pivot(index="date", columns="name", values="value").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide


def load_board_flows(start: str | None = None, end: str | None = None, period: str = "1d") -> pd.DataFrame:
    """板块主力净流入宽表，单位与成交额一致，均为元。"""
    sql = [
        "SELECT f.trade_date AS date, b.name, f.main_net AS value",
        f"FROM {db.qualified('board_fund_flow')} f",
        f"JOIN {db.qualified('board')} b USING (board_id)",
        "WHERE f.period = %s",
    ]
    params: list[object] = [period]
    if start:
        sql.append("AND f.trade_date >= %s")
        params.append(start)
    if end:
        sql.append("AND f.trade_date <= %s")
        params.append(end)
    frame = db.query_df(" ".join(sql), params)
    if frame.empty:
        return pd.DataFrame()
    wide = frame.pivot(index="date", columns="name", values="value").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide


def load_market_flow(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    sql = [f"SELECT * FROM {db.qualified('market_fund_flow')}"]
    params: list[object] = []
    conditions = []
    if start:
        conditions.append("trade_date >= %s")
        params.append(start)
    if end:
        conditions.append("trade_date <= %s")
        params.append(end)
    if conditions:
        sql.append("WHERE " + " AND ".join(conditions))
    sql.append("ORDER BY trade_date")
    frame = db.query_df(" ".join(sql), params)
    if not frame.empty:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.set_index("trade_date")
        for col in ("main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net"):
            if col in frame.columns and col.endswith("_net"):
                frame[col] = frame[col] / 1e8
    return frame


def load_news(start: str | None = None, end: str | None = None, limit: int = 500, *, cutoff=None) -> pd.DataFrame:
    """按发布日期取新闻（含 end 当天全天）。

    ``published_at`` 是 timestamptz，直接和 '2026-10-30' 比较会被当成当天 00:00，
    把当天白天的新闻全漏掉——所以统一按日期比较。
    """
    sql = [f"SELECT * FROM {db.qualified('news_item')}"]
    params: list[object] = []
    conditions = []
    if start:
        conditions.append("published_at::date >= %s::date")
        params.append(start)
    if end:
        conditions.append("published_at::date <= %s::date")
        params.append(end)
    if cutoff:
        conditions.append("published_at <= %s")
        params.append(cutoff)
    if conditions:
        sql.append("WHERE " + " AND ".join(conditions))
    sql.append("ORDER BY published_at DESC LIMIT %s")
    params.append(limit)
    return db.query_df(" ".join(sql), params)
