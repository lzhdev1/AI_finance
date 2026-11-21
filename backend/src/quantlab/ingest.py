"""入库层：把 sources 抓回来的 DataFrame 写进 PostgreSQL，并记录流水线与质量检查。

三条原则：
1. 一切写入靠业务主键幂等（``ON CONFLICT``），重跑不会产生脏数据；
2. 每次抓取都包在 ``phase()`` 里，成功/部分成功/失败都留痕到 ``pipeline_run``；
3. 遇到 ``RateLimited`` 立即结束本轮并标 partial —— 下次再补，不硬扛。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb

from . import db, realtime, sources
from .config import load_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 流水线留痕

@contextmanager
def phase(name: str, run_date: date | None = None) -> Iterator[dict]:
    run_date = run_date or date.today()
    row = db.query_one(
        f"INSERT INTO {db.qualified('pipeline_run')} (run_date, phase) VALUES (%s, %s) RETURNING run_id",
        [run_date, name],
    )
    run_id = int(row["run_id"])
    stats: dict = {}
    status = "success"
    error: str | None = None
    try:
        yield stats
        if stats.get("warnings"):
            status = "partial"
    except sources.RateLimited as exc:
        status, error = "partial", str(exc)
        log.warning("[%s] 因限流中止本轮: %s", name, exc)
    except Exception as exc:
        status, error = "failed", str(exc)
        log.exception("[%s] 失败", name)
        raise
    finally:
        try:
            db.execute(
                f"UPDATE {db.qualified('pipeline_run')} "
                "SET status = %s, finished_at = now(), stats = %s, error = %s WHERE run_id = %s",
                [status, Jsonb(stats), error, run_id],
            )
        except Exception:  # noqa: BLE001
            log.exception("流水线状态回写失败 run_id=%s", run_id)
        if status == "success":
            log.info("[%s] 完成: %s", name, stats)


def _none(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _rows(frame: pd.DataFrame, columns: Sequence[str]) -> list[tuple]:
    return [tuple(_none(row[col]) for col in columns) for _, row in frame.iterrows()]


# ---------------------------------------------------------------- 日历

def update_calendar() -> int:
    with phase("calendar") as stats:
        frame = sources.fetch_trade_calendar()
        written = db.upsert(
            "trade_calendar",
            ["trade_date", "source"],
            [(row.trade_date, "sina") for row in frame.itertuples()],
            conflict=["trade_date"],
            update=["source"],
        )
        stats["rows"] = written
        return written


# ---------------------------------------------------------------- 基金净值

def active_instruments(codes: Sequence[str] | None = None) -> list[dict]:
    """Refresh the active universe, held positions, and funds in unconfirmed instructions."""
    return db.query(
        f"""WITH manager AS (
                SELECT pod_id FROM {db.qualified('pod')} WHERE strategy_key = 'agent_manager'
            ), held AS (
                SELECT instrument_id FROM {db.qualified('pod_order')} JOIN manager USING (pod_id)
                WHERE execute_on IS NOT NULL GROUP BY instrument_id
                HAVING sum(CASE WHEN side = 'buy' THEN shares ELSE -shares END) > 0
            ), pending AS (
                SELECT d.trade_date, elem->>'code' code FROM {db.qualified('pod_decision')} d
                JOIN manager USING (pod_id)
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(d.payload->'pending', '[]'::jsonb)) elem
                WHERE d.executed_at IS NULL
            )
            SELECT i.instrument_id, i.code, i.name, i.kind, latest.day AS last_nav,
                   least((SELECT min(trade_date) FROM pending WHERE code = i.code),
                         CASE WHEN h.instrument_id IS NOT NULL THEN
                           (SELECT min(d.trade_date) FROM {db.qualified('pod_decision')} d
                            JOIN manager USING(pod_id) WHERE d.executed_at IS NULL) END) AS pending_day
            FROM {db.qualified('instrument')} i LEFT JOIN held h USING(instrument_id)
            LEFT JOIN LATERAL (SELECT max(trade_date) AS day FROM {db.qualified('instrument_nav')} n
                               WHERE n.instrument_id=i.instrument_id) latest ON true
            WHERE (i.code = ANY(%s) OR (%s::text[] IS NULL AND
                   (i.is_active OR h.instrument_id IS NOT NULL OR EXISTS (SELECT 1 FROM pending WHERE code=i.code))))
            ORDER BY pending_day NULLS LAST, latest.day NULLS FIRST, i.code""",
        [list(codes) if codes is not None else None] * 2,
    )


def update_fund_nav(codes: Sequence[str] | None = None, *, required_dates=None) -> dict[str, int]:
    """Repair pending dates and ingest incremental NAVs, keeping other funds independent."""
    with phase("fund_nav") as stats:
        instruments = active_instruments(codes)
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        last_close = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
        last_session = db.query_one(
            f"SELECT max(trade_date) AS day FROM {db.qualified('trade_calendar')} WHERE trade_date <= %s",
            [last_close],
        )["day"]
        navs = db.qualified("instrument_nav")
        written = stats["funds"] = {}
        preferred = "eastmoney-lsjz"
        for item in instruments:
            latest, pending = item["last_nav"], item["pending_day"]
            needed = (required_dates or {}).get(item["code"])
            if needed:
                pending = min(pending, needed) if pending else needed
            if latest and last_session == latest and not pending:
                continue
            # Overlap recent rows to pick up revised NAVs. An outstanding instruction
            # may need a much earlier day, even after more recent NAVs have arrived.
            start = latest - timedelta(days=7) if latest else None
            if pending and start:
                start = min(pending, start)
            try:
                frame = sources.fetch_fund_nav(item["code"], since=start, preferred=preferred)
            except sources.RateLimited:
                raise
            except Exception as exc:
                stats.setdefault("warnings", []).append(f"{item['code']} 净值读取失败（{type(exc).__name__}）")
                continue
            if frame.empty:
                stats.setdefault("warnings", []).append(f"{item['code']} 没有可用的单位净值")
                continue
            if last_session and frame["trade_date"].max() < last_session:
                stats.setdefault("warnings", []).append(f"{item['code']} 最新净值截至 {frame['trade_date'].max()}")
            source = frame.attrs["source"]
            preferred = source
            count = db.execute_many(
                f"""INSERT INTO {navs} AS n (instrument_id,trade_date,nav,acc_nav,source)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(instrument_id,trade_date) DO UPDATE
                    SET nav=excluded.nav, acc_nav=coalesce(excluded.acc_nav,n.acc_nav),
                        source=excluded.source, ingested_at=now()
                    WHERE (n.nav,n.acc_nav) IS DISTINCT FROM
                          (excluded.nav,coalesce(excluded.acc_nav,n.acc_nav))""",
                [(item["instrument_id"], r.trade_date, r.nav, _none(r.acc_nav), source) for r in frame.itertuples()
                 if r.trade_date <= last_close],
            )
            written[item["code"]] = count
            log.info("%s %s 净值更新 %s 行，截至 %s", item["code"], source, count, frame["trade_date"].max())
        return written


# ---------------------------------------------------------------- 新闻

def update_news(sources_list: Sequence[str] | None = None, *, audit: dict | None = None) -> int:
    settings = load_settings()
    wanted = list(sources_list or settings.news_sources)
    with phase("news") as stats:
        total = 0
        for source in wanted:
            try:
                fetch = {"cls": sources.fetch_news_cls, "eastmoney": sources.fetch_news_em}[source]
                frame = fetch()
            except Exception as exc:
                stats.setdefault("warnings", []).append(f"{source}: {type(exc).__name__}")
                log.warning("新闻源 %s 不可用：%s", source, type(exc).__name__)
                continue
            if frame.empty:
                stats.setdefault("warnings", []).append(f"{source} 未返回新闻")
                continue
            columns = ["source", "external_id", "published_at", "title", "body", "url", "content_hash"]
            count = db.upsert(
                "news_item", columns, _rows(frame, columns), conflict=["content_hash"], update=[]
            )
            stats[source] = {"rows": len(frame), "inserted": count}
            total += count
        stats["inserted_total"] = total
        if audit is not None:
            audit.update(stats)
        return total


# ---------------------------------------------------------------- 板块

def update_boards(kinds: Sequence[str] | None = None, prefer: str = "ths") -> int:
    """板块清单。默认走同花顺（不受 push2 限流影响），失败再退回东方财富。"""
    settings = load_settings()
    kinds = list(kinds or settings.board_kinds)
    with phase("boards") as stats:
        written = 0
        for kind in kinds:
            frame, used = None, None
            if prefer == "ths":
                try:
                    frame, used = sources.fetch_boards_ths(kind), "ths"
                except Exception as exc:  # noqa: BLE001
                    log.warning("同花顺 %s 板块清单失败，改用东方财富：%s", kind, exc)
            if frame is None:
                frame, used = sources.fetch_boards(kind), "eastmoney"
            count = db.upsert(
                "board",
                ["code", "name", "kind", "source"],
                [(row.code, row.name, kind, used) for row in frame.itertuples()],
                conflict=["source", "code"],
                update=["name", "kind"],
            )
            stats[kind] = {"rows": len(frame), "upserted": count, "source": used}
            written += count
        return written


def _align(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """补齐缺失列，避免不同数据源列不一致时写库报错。"""
    out = frame.copy()
    for column in columns:
        if column not in out.columns:
            out[column] = None
    return out


def board_map() -> dict[str, dict]:
    """板块名称 → {board_id, code, kind}。"""
    return {
        row["name"]: row
        for row in db.query(f"SELECT board_id, code, name, kind FROM {db.qualified('board')}")
    }


def _latest_closed_session() -> date:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)
    day = db.query_one(f"SELECT max(trade_date) AS day FROM {db.qualified('trade_calendar')} WHERE trade_date <= %s", [cutoff])["day"]
    if not day:
        raise ValueError("缺少最近已收盘交易日")
    return day


def update_board_quote(limit: int | None = 20, lookback_days: int = 30, kinds: Sequence[str] | None = None) -> dict[str, int]:
    """增量抓板块日线。每次最多 limit 个板块，优先补最久没更新的。"""
    settings = load_settings()
    kinds = list(kinds or settings.board_kinds)
    with phase("board_quote") as stats:
        latest = _latest_closed_session()
        rows = db.query(
            f"""
            SELECT b.board_id, b.name, b.kind, b.source, max(q.trade_date) AS last_date
            FROM {db.qualified('board')} b
            LEFT JOIN {db.qualified('board_quote')} q USING (board_id)
            WHERE b.is_active AND b.kind = ANY(%s)
            GROUP BY b.board_id, b.name, b.kind, b.source
            HAVING max(q.trade_date) IS NULL OR max(q.trade_date) < %s
            ORDER BY EXISTS (SELECT 1 FROM {db.qualified('instrument_board')} ib
                             WHERE ib.board_id = b.board_id) DESC,
                     max(q.trade_date) NULLS FIRST, b.board_id
            LIMIT %s
            """,
            [kinds, latest, limit],
        )
        end = latest.strftime("%Y%m%d")
        done: dict[str, int] = {}
        for item in rows:
            start = (
                item["last_date"].strftime("%Y%m%d")
                if item["last_date"]
                else (latest - timedelta(days=lookback_days * 10)).strftime("%Y%m%d")
            )
            if start > end:
                continue
            try:
                if item["source"] == "ths":
                    frame = sources.fetch_board_hist_ths(item["name"], item["kind"], start, end)
                elif item["kind"] == "concept":
                    frame = sources.fetch_board_hist_concept(item["name"], start, end)
                else:
                    frame = sources.fetch_board_hist(item["name"], start, end)
            except sources.RateLimited:
                raise
            except Exception as exc:
                stats.setdefault("warnings", []).append(f"{item['name']}: {type(exc).__name__}")
                continue
            if frame.empty:
                done[item["name"]] = 0
                continue
            if item["last_date"]:
                frame = frame[frame["trade_date"] > item["last_date"]]
            columns = ["board_id", "trade_date", "open", "high", "low", "close", "pct_chg", "amplitude", "turnover_rate", "amount", "volume", "source"]
            payload = _align(frame, columns)
            payload["board_id"] = item["board_id"]
            payload["source"] = item["source"]
            count = db.upsert(
                "board_quote", columns, _rows(payload, columns), conflict=["board_id", "trade_date"],
                update=["open", "high", "low", "close", "pct_chg", "amplitude", "turnover_rate", "amount", "volume", "ingested_at"],
            )
            done[item["name"]] = count
        stats["boards"] = done
        stats["count"] = len(done)
        return done


def update_board_flow(
    indicator: str = "今日",
    sector_type: str = "行业资金流",
    period: str = "1d",
    trade_date: date | None = None,
    prefer: str = "ths",
) -> int:
    """板块资金流截面（一次请求覆盖全部板块）。

    两个源的口径不同：同花顺只给流入/流出/净额，东方财富给超大/大/中/小单四档。
    统一落到同一张表，缺失档位留 NULL —— 宁可空着，也不要拿净额去冒充四档。
    """
    kind = "industry" if "行业" in sector_type else "concept"
    closed = _latest_closed_session()
    if trade_date and trade_date != closed:
        raise ValueError("即时资金表仅用于最近收盘日，不能回填指定历史日期")
    sessions = {"1d": ("今日", 1), "5d": ("5日", 5), "10d": ("10日", 10)}
    if period not in sessions or sessions[period][0] != indicator:
        raise ValueError("资金榜周期须与指标一致")
    trade_date = closed

    with phase(f"board_flow_{kind}") as stats:
        frame, used = None, None
        if prefer == "ths":
            try:
                frame, used = sources.fetch_board_flow_ths(kind, period), "ths"
            except Exception as exc:  # noqa: BLE001
                log.warning("同花顺 %s 资金流失败，改用东方财富：%s", kind, exc)
        if frame is None:
            frame, used = sources.fetch_board_flow_rank(indicator=indicator, sector_type=sector_type), "eastmoney"

        if "main_net_pct" not in frame.columns:
            gross = frame.get("inflow", 0) + frame.get("outflow", 0)
            frame["main_net_pct"] = (frame["main_net"] / gross.replace(0, float("nan")) * 100).round(3)

        # The vendor ranks are undated live snapshots. Verify against the dated
        # close on each individual row before assigning it to this session.
        anchors = db.query_df(
            f"""WITH prior AS (SELECT trade_date FROM {db.qualified('trade_calendar')}
                    WHERE trade_date < %s ORDER BY trade_date DESC OFFSET %s LIMIT 1)
                SELECT b.board_id,b.name,q.close::float reference_close,
                       (100*(q.close/nullif(p.close,0)-1))::float reference_pct
                FROM {db.qualified('board')} b JOIN {db.qualified('board_quote')} q USING(board_id)
                LEFT JOIN {db.qualified('board_quote')} p ON p.board_id=q.board_id
                     AND p.trade_date=(SELECT trade_date FROM prior) AND p.source=q.source
                WHERE b.is_active AND b.kind=%s AND b.source=%s AND q.source=%s AND q.trade_date=%s""",
            [trade_date, sessions[period][1] - 1, kind, used, used, trade_date],
        )
        payload = frame.merge(anchors, on="name", validate="one_to_one").dropna(subset=["main_net"])
        if used == "ths":
            # The published index column is rounded to six significant figures.
            tolerance = (payload.reference_close.abs() * 0.0000051).clip(lower=0.005001)
            matched = (payload.snapshot_close - payload.reference_close).abs() <= tolerance
        else:
            matched = (payload.close_pct - payload.reference_pct).abs() <= 0.0051
        if len(payload) < 3 or matched.mean() < 0.9:
            stats["warnings"] = [f"资金榜与{trade_date}同源收盘不能对应，仅核对到{int(matched.sum())}/{len(payload)}"]
            return 0
        payload = payload[matched]
        columns = ["board_id", "trade_date", "period", "main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net", "close_pct", "source"]
        payload = _align(payload, columns)
        payload["trade_date"] = trade_date
        payload["period"] = period
        payload["source"] = used + "-close-verified"
        unmatched = len(frame) - len(payload)
        values = columns[3:]
        count = db.execute_many(
            f"""INSERT INTO {db.qualified('board_fund_flow')} AS f ({','.join(columns)})
            VALUES ({','.join(['%s'] * len(columns))}) ON CONFLICT (board_id,trade_date,period)
            DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in values)},ingested_at=now()
            WHERE ({','.join('f.'+c for c in values)}) IS DISTINCT FROM
            ({','.join('excluded.'+c for c in values)})""", _rows(payload, columns),
        )
        stats.update({"rows": len(frame), "verified": len(payload), "trade_date": str(closed),
                      "upserted": count, "unmatched": unmatched, "source": used})
        if unmatched:
            stats["warnings"] = [f"{unmatched} 项未匹配同源板块、收盘价或有效净额"]
        return count


def update_market_flow() -> int:
    with phase("market_flow") as stats:
        frame = sources.fetch_market_flow()
        columns = ["trade_date", "main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net", "close_pct", "source"]
        payload = frame.copy()
        payload["source"] = "eastmoney"
        count = db.upsert(
            "market_fund_flow", columns, _rows(payload, columns), conflict=["trade_date"],
            update=["main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net", "close_pct", "ingested_at"],
        )
        stats["upserted"] = count
        return count


def update_index(code: str | None = None) -> int:
    """日线收盘价入库。

    默认抓满「上证 / 深证 / 创业板 / 沪深300」四只——顶部行情条要四个，
    只存基准那一只的话，周末没有盘中快照时页面上就只剩沪深300。
    """
    codes = [code] if code else list(realtime.INDEX_CODES)
    total = 0
    for item in codes:
        with phase(f"index_{item}") as stats:
            frame = sources.fetch_index_daily(item)
            columns = ["index_code", "trade_date", "close", "pct_chg", "source"]
            payload = frame.copy()
            payload["index_code"] = item
            count = db.upsert(
                "index_quote", columns, _rows(payload, columns), conflict=["index_code", "trade_date"],
                update=["close", "pct_chg", "source", "ingested_at"],
            )
            stats["upserted"] = count
            total += count
    return total


# ---------------------------------------------------------------- 质量门禁

def check_nav_quality() -> list[dict]:
    """净值质量：最新净值日相对"最新的已发生交易日"落后几天。

    注意：交易日历里含未来日期（年末排期），基准必须取 ``<= current_date`` 的最大值，
    否则计算出的滞后天数会是负向放大的一堆噪声。
    """
    results: list[dict] = []
    latest_cal = db.query_one(
        f"SELECT max(trade_date) AS d FROM {db.qualified('trade_calendar')} WHERE trade_date <= current_date"
    )
    cal_latest = latest_cal["d"] if latest_cal else None

    for row in db.query(
        f"""
        SELECT i.code, count(n.trade_date) AS rows, max(n.trade_date) AS last_date, min(n.trade_date) AS first_date
        FROM {db.qualified('instrument')} i
        LEFT JOIN {db.qualified('instrument_nav')} n USING (instrument_id)
        WHERE i.is_active
        GROUP BY i.code ORDER BY i.code
        """,
    ):
        lag = (cal_latest - row["last_date"]).days if cal_latest and row["last_date"] else None
        status = "pass"
        if lag is None or lag > 3:
            status = "fail"
        elif lag > 1:
            status = "warn"
        results.append(
            {
                "target": f"instrument_nav:{row['code']}",
                "check_name": "lag_days",
                "status": status,
                "metric": lag,
                "detail": {"rows": row["rows"], "first": str(row["first_date"]) if row["first_date"] else None,
                           "last": str(row["last_date"]) if row["last_date"] else None},
            }
        )
    return results


def record_quality(run_date: date, results: Sequence[dict]) -> None:
    if not results:
        return
    db.upsert(
        "data_quality",
        ["run_date", "target", "check_name", "status", "metric", "detail"],
        [
            (run_date, item["target"], item["check_name"], item["status"], item["metric"], Jsonb(item["detail"]))
            for item in results
        ],
        conflict=["run_date", "target", "check_name"],
        update=["status", "metric", "detail", "checked_at"],
    )


def freshness() -> dict[str, object]:
    """各核心表的最新日期，供看板与日志展示。"""
    queries = {
        "trade_calendar": f"SELECT max(trade_date) AS d FROM {db.qualified('trade_calendar')}",
        "instrument_nav": f"SELECT max(trade_date) AS d FROM {db.qualified('instrument_nav')}",
        "board_quote": f"SELECT max(trade_date) AS d FROM {db.qualified('board_quote')}",
        "board_fund_flow": f"SELECT max(trade_date) AS d FROM {db.qualified('board_fund_flow')}",
        "market_fund_flow": f"SELECT max(trade_date) AS d FROM {db.qualified('market_fund_flow')}",
        "index_quote": f"SELECT max(trade_date) AS d FROM {db.qualified('index_quote')}",
        "news_item": f"SELECT max(published_at) AS d FROM {db.qualified('news_item')}",
    }
    return {name: (db.query_one(sql) or {}).get("d") for name, sql in queries.items()}
