"""Prepare reproducible inputs for a dedicated replay database. Never writes to the source."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import psycopg
import requests
from bs4 import BeautifulSoup
from psycopg import sql

from . import db

SH = ZoneInfo("Asia/Shanghai")
ARCHIVE = "https://www.chinanews.com.cn/scroll-news/{:%Y/%m%d}/news.shtml"
SOURCE = "chinanews-finance"


def verify_databases(source: str, target: str) -> None:
    if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", target) or source == target:
        raise ValueError("历史输入只能写入单独的 quantlab_replay_* 数据库")


def copy_market(source: str, target: str, start: date, end: date, *, frozen: bool = False) -> dict:
    """Copy a consistent read-only snapshot. Preserve record IDs, provenance and ingest timestamps."""
    verify_databases(source, target)
    warmup = start - timedelta(days=370)
    tables = {
        "trade_calendar": ("trade_date BETWEEN %s AND %s", (warmup, end + timedelta(days=35))),
        "board": ("true", ()),
        "instrument": ("true", ()),
        "instrument_fee": ("true", ()),
        "instrument_board": ("true", ()),
        **{
            table: ("trade_date BETWEEN %s AND %s", (warmup, end))
            for table in (
                "instrument_nav",
                "index_quote",
                "board_quote",
                "board_fund_flow",
                "market_fund_flow",
            )
        },
    }
    sequences = [("instrument", "instrument_id"), ("board", "board_id")]
    if frozen:
        if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", source):
            raise ValueError("对照回放只接受已归档的回放库作为输入源")
        # Include the original IDs and timestamps used in citations. Account records,
        # forecasts and price evidence obtained after the run are deliberately separate.
        tables.update(
            {table: ("true", ()) for table in ("news_item", "board_intraday", "index_intraday")}
        )
        sequences.append(("news_item", "news_id"))
    counts = {}
    with (
        psycopg.connect(db.dsn(), dbname=source) as reader,
        psycopg.connect(db.dsn(), dbname=target) as writer,
    ):
        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if frozen:
            writer.execute("SELECT pg_advisory_xact_lock(794022005)")
            if writer.execute(f"SELECT 1 FROM {db.qualified('pod')} LIMIT 1").fetchone():
                raise ValueError("冻结输入只能装入尚未开户的回放库")
        for table, (where, params) in tables.items():
            destination = sql.Identifier(db.schema(), table)
            cols = [
                r[0]
                for r in reader.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    [db.schema(), table],
                ).fetchall()
            ]
            names = sql.SQL(",").join(map(sql.Identifier, cols))
            writer.execute(
                sql.SQL("CREATE TEMP TABLE replay_input (LIKE {}) ON COMMIT DROP").format(
                    destination
                )
            )
            source_query = sql.SQL("COPY (SELECT {} FROM {} WHERE {}) TO STDOUT").format(
                names, destination, sql.SQL(where)
            )
            into_staging = sql.SQL("COPY replay_input ({}) FROM STDIN").format(names)
            with (
                reader.cursor().copy(source_query, params) as out,
                writer.cursor().copy(into_staging) as into,
            ):
                for chunk in out:
                    into.write(chunk)
            count = writer.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) SELECT {} FROM replay_input ON CONFLICT DO NOTHING"
                ).format(destination, names, names)
            ).rowcount
            counts[table] = count
            if frozen:
                # A retry may encounter rows from a different snapshot with the same key.
                # Compare both directions before committing any of the imported tables.
                changed = writer.execute(
                    sql.SQL("""
                    SELECT EXISTS(
                        (SELECT {cols} FROM replay_input EXCEPT SELECT {cols} FROM {dest})
                        UNION ALL
                        (SELECT {cols} FROM {dest} EXCEPT SELECT {cols} FROM replay_input)
                    )
                """).format(cols=names, dest=destination)
                ).fetchone()[0]
                if changed:
                    raise ValueError(f"{table} 与原版冻结输入有差异")
            writer.execute("DROP TABLE replay_input")
        # Copied natural-reference IDs must not collide with a subsequent reference-data insert.
        for table, column in sequences:
            writer.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s,%s), coalesce(max({}),1), count(*)>0) FROM {}"
                ).format(sql.Identifier(column), sql.Identifier(db.schema(), table)),
                [db.qualified(table), column],
            )
    return counts


def parse_finance(day: date, html: str) -> list[tuple]:
    """Parse the publisher's dated rolling index. Use its article timestamps, never fetch time."""
    archive_url = ARCHIVE.format(day)
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one(".btl")
    if not header or day.isoformat() not in header.get_text(" ", strip=True):
        raise ValueError(f"{day} 归档目录的年月日不符")
    by_url = {}
    for item in soup.select("li"):
        category = item.select_one(".dd_lm")
        if category is None or category.get_text(strip=True).strip("[]【】") != "财经":
            continue
        article, stamp = item.select_one(".dd_bt a[href]"), item.select_one(".dd_time")
        if article is None or stamp is None:
            continue
        match = re.fullmatch(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", stamp.get_text(strip=True))
        if not match:
            continue
        month, number, hour, minute = map(int, match.groups())
        if (month, number) != (day.month, day.day) or hour >= 24 or minute >= 60:
            continue
        url = urlsplit(urljoin(archive_url, article["href"]))
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or not (
                url.hostname == "chinanews.com.cn" or url.hostname.endswith(".chinanews.com.cn")
            )
        ):
            continue
        canonical = urlunsplit(("https", url.hostname, url.path, url.query, ""))
        if not re.fullmatch(rf"/[^/]+/{day:%Y/%m-%d}/[0-9]+\.shtml", url.path):
            continue
        title = article.get_text(" ", strip=True)
        if not title or "\ufffd" in title:
            continue
        at = datetime.combine(day, time(hour, minute), SH)
        by_url[canonical] = (
            SOURCE,
            canonical,
            at,
            title,
            canonical,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
    return sorted(by_url.values(), key=lambda r: (r[2], r[4]))


def fetch_archive(day: date) -> list[tuple]:
    response = requests.get(ARCHIVE.format(day), timeout=(5, 15))
    response.raise_for_status()
    items = parse_finance(day, response.content.decode("utf-8", errors="replace"))
    if not items:
        raise ValueError(f"{day} 中新网目录没有可核验的财经标题")
    return items


def load_news(source: str, target: str, start: date, end: date) -> dict:
    verify_databases(source, target)
    start -= timedelta(days=3)
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    with psycopg.connect(db.dsn(), dbname=target, autocommit=True) as conn:
        existing = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT (published_at AT TIME ZONE 'Asia/Shanghai')::date FROM {db.qualified('news_item')} WHERE source=%s",
                [SOURCE],
            )
        }
        needed = [day for day in days if day not in existing]
        failures = {}
        written = 0

        def fetch(day):
            try:
                return day, fetch_archive(day), ""
            except (requests.RequestException, ValueError) as exc:
                return day, [], f"{type(exc).__name__}: {str(exc)[:160]}"

        # Three concurrent requests to one daily index each. Articles stay on the publisher's site.
        with ThreadPoolExecutor(3) as pool:
            for day, records, error in pool.map(fetch, needed):
                if error:
                    failures[str(day)] = error
                    continue
                with conn.transaction(), conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO {db.qualified('news_item')} (source,external_id,published_at,title,url,content_hash) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(content_hash) DO NOTHING",
                        records,
                    )
                    written += cur.rowcount
                if day.day in (1, 15):
                    print(f"{day} 累计新增 {written} 条财经标题", flush=True)
    return {"inserted": written, "cached_days": len(days) - len(needed), "failures": failures}


def coverage(target: str, start: date, end: date) -> dict:
    verify_databases("", target)
    with psycopg.connect(db.dsn(), dbname=target) as conn:
        rows = conn.execute(
            f"""
            SELECT c.trade_date,
              (SELECT count(*) FROM {db.qualified("instrument_nav")} n WHERE n.trade_date=c.trade_date) funds,
              (SELECT count(*) FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
               WHERE q.trade_date=c.trade_date AND b.kind='industry') industries,
              (SELECT count(*) FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
               WHERE q.trade_date=c.trade_date AND b.kind='concept') concepts,
              EXISTS(SELECT 1 FROM {db.qualified("index_quote")} q WHERE q.trade_date=c.trade_date AND q.index_code='000300' AND q.close>0) benchmark,
              (SELECT count(*) FROM {db.qualified("news_item")} n WHERE n.source=%s AND n.url IS NOT NULL
               AND n.published_at > ((c.trade_date-3)+time '14:30') AT TIME ZONE 'Asia/Shanghai'
               AND n.published_at < (c.trade_date+time '14:30') AT TIME ZONE 'Asia/Shanghai') news_available
            FROM {db.qualified("trade_calendar")} c WHERE c.trade_date BETWEEN %s AND %s ORDER BY c.trade_date
        """,
            [SOURCE, start, end],
        ).fetchall()
        anchor = conn.execute(
            f"SELECT max(q.trade_date) FROM {db.qualified('index_quote')} q WHERE q.trade_date < %s AND index_code='000300'",
            [start],
        ).fetchone()[0]
        articles = conn.execute(
            f"SELECT count(*), count(DISTINCT (published_at AT TIME ZONE 'Asia/Shanghai')::date) FROM {db.qualified('news_item')} "
            "WHERE source=%s AND (published_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s",
            [SOURCE, start - timedelta(days=3), end],
        ).fetchone()
    return {
        "sessions": len(rows),
        "benchmark_anchor": str(anchor),
        "benchmark_sessions": sum(r[4] for r in rows),
        "news_articles": articles[0],
        "news_archive_days": articles[1],
        "valid_news_sessions": sum(r[5] > 0 for r in rows),
        "daily": [
            dict(zip(("date", "funds", "industries", "concepts", "benchmark", "news_72h"), r))
            for r in rows
        ],
    }


def compare_closes(stored: dict, independent: dict, expected: list[date]) -> dict:
    deviations = {}
    for day in expected:
        if day not in stored or day not in independent:
            raise ValueError(f"{day} 无法取得两份指数收盘记录")
        left, right = (Decimal(str(x)) for x in (stored[day], independent[day]))
        if not left.is_finite() or not right.is_finite() or min(left, right) <= 0:
            raise ValueError(f"{day} 指数收盘点位无效")
        deviations[day] = abs(left - right)
        if deviations[day] > Decimal("0.0051"):
            raise ValueError(f"{day} 沪深300收盘不符：{left} / {right}")
    return {"matched": len(expected), "max_difference_points": str(max(deviations.values()))}


def verify_benchmark(target: str, start: date, end: date) -> dict:
    """Compare each replay session and its starting anchor to the official index publisher."""
    verify_databases("", target)
    with psycopg.connect(db.dsn(), dbname=target) as conn:
        dates = [
            r[0]
            for r in conn.execute(
                f"""SELECT trade_date FROM {db.qualified("trade_calendar")}
                WHERE trade_date BETWEEN (SELECT max(trade_date) FROM {db.qualified("trade_calendar")}
                                          WHERE trade_date < %s) AND %s ORDER BY trade_date""",
                [start, end],
            )
        ]
        stored = dict(
            conn.execute(
                f"SELECT trade_date, close FROM {db.qualified('index_quote')} WHERE index_code='000300' AND trade_date=ANY(%s)",
                [dates],
            )
        )
    if len(dates) < 2 or dates[0] >= start:
        raise ValueError("核验需要窗口内交易日和窗口前基准锚点")
    response = requests.get(
        "https://www.csindex.com.cn/csindex-home/perf/index-perf",
        params={
            "indexCode": "000300",
            "startDate": dates[0].strftime("%Y%m%d"),
            "endDate": dates[-1].strftime("%Y%m%d"),
        },
        headers={"Referer": "https://www.csindex.com.cn/"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    official = {}
    for row in response.json()["data"]:
        if row["indexCode"] != "000300":
            raise ValueError("指数发布方返回的代码与沪深300不符")
        dt = datetime.strptime(row["tradeDate"], "%Y%m%d").date()
        if dt in official:
            raise ValueError(f"{dt} 发布方返回重复收盘记录")
        official[dt] = row["close"]
    return {
        **compare_closes(stored, official, dates),
        "source": response.url,
        "closes": {
            str(d): {"stored": str(stored[d]), "publisher": official[d]}
            for d in [dates[0], dates[1], dates[-1]]
        },
    }


def main():
    p = argparse.ArgumentParser(description="准备隔离回放数据库的行情及历史新闻")
    p.add_argument("--source-db", default="quantlab")
    p.add_argument("--target-db", required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument(
        "--phase", choices=["market", "frozen", "news", "audit", "verify", "all"], default="all"
    )
    a = p.parse_args()
    verify_databases(a.source_db, a.target_db)
    if a.start > a.end:
        p.error("start must precede end")
    if a.phase in ("market", "frozen", "all"):
        print(
            json.dumps(
                copy_market(a.source_db, a.target_db, a.start, a.end, frozen=a.phase == "frozen")
            ),
            flush=True,
        )
    if a.phase in ("news", "all"):
        print(
            json.dumps(load_news(a.source_db, a.target_db, a.start, a.end), ensure_ascii=False),
            flush=True,
        )
    if a.phase in ("audit", "all"):
        print(json.dumps(coverage(a.target_db, a.start, a.end), default=str))
    if a.phase == "verify":
        print(json.dumps(verify_benchmark(a.target_db, a.start, a.end), ensure_ascii=False))


if __name__ == "__main__":
    main()
