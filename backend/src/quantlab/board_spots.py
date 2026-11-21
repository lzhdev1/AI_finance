"""Vendor-stamped intraday observations for the exact index in the live catalog."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from http.client import HTTPException
from math import isfinite
from zoneinfo import ZoneInfo

import pandas as pd
from bs4 import BeautifulSoup

from . import db, usage

SH = ZoneInfo("Asia/Shanghai")
log = logging.getLogger(__name__)


def ths_shortlist(as_of, limit=16):
    """Sample current holdings and the last watchlist first, then large prior-session flows."""
    t = db.qualified
    return db.query(
        f"""WITH latest AS (
                SELECT payload FROM {t("agent_research")} WHERE as_of<=%s
                  AND payload->>'run_mode'='live' ORDER BY as_of DESC,report_id DESC LIMIT 1
            ), watched AS (
                SELECT (w->>'board_id')::int board_id FROM latest,
                  jsonb_array_elements(payload->'watchlist') w
            ), held AS (
                SELECT DISTINCT ib.board_id FROM {t("pod")} p JOIN {t("pod_position")} pos USING(pod_id)
                JOIN {t("instrument_board")} ib USING(instrument_id)
                WHERE p.strategy_key='agent_manager' AND p.status='active' AND pos.weight>0
                  AND pos.trade_date=(SELECT max(v.trade_date) FROM {t("pod_nav")} v
                        WHERE v.pod_id=p.pod_id AND v.trade_date<%s)
            )
            SELECT b.code,b.name,b.kind FROM {t("board")} b
            LEFT JOIN {t("board_fund_flow")} f ON f.board_id=b.board_id AND f.period='1d'
              AND f.source='ths-close-verified' AND f.ingested_at<=%s
              AND f.trade_date=(SELECT max(trade_date) FROM {t("trade_calendar")} WHERE trade_date<%s)
            WHERE b.is_active AND b.source='ths'
            ORDER BY b.board_id IN (SELECT board_id FROM held) DESC,
                     b.board_id IN (SELECT board_id FROM watched) DESC,abs(f.main_net) DESC NULLS LAST,b.board_id
            LIMIT %s""",
        [as_of, as_of.date(), as_of, as_of.date(), limit],
    )


def ths_minutes(board, read):
    """Resolve a concept's official quote code and parse the timestamp of its last actual price."""
    code = board["code"]
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    quote_code = code
    if board["kind"] == "concept":
        url = f"https://q.10jqka.com.cn/gn/detail/code/{code}/"
        detail = read(url, referer="https://q.10jqka.com.cn/", encoding="gbk", timeout=3)
        hidden = BeautifulSoup(detail, "lxml").select_one("input#clid")
        quote_code = hidden.get("value", "") if hidden else ""
        if not re.fullmatch(r"[0-9]{6}", quote_code):
            return None
    url = f"https://d.10jqka.com.cn/v4/time/bk_{quote_code}/last.js"
    raw = read(url, referer="https://q.10jqka.com.cn/", encoding="gbk", timeout=3)
    prefix = f"quotebridge_v4_time_bk_{quote_code}_last("
    body = raw.strip().removesuffix(";")
    if not body.startswith(prefix) or not body.endswith(")"):
        return None
    quote = json.loads(body[len(prefix) : -1]).get("bk_" + quote_code) or {}
    if quote.get("name") != board["name"] or not quote.get("data"):
        return None
    for line in reversed(quote["data"].split(";")):
        values = line.split(",")
        if len(values) < 2 or not re.fullmatch(r"[0-9]{4}", values[0]):
            continue
        price = float(values[1])
        previous = float(quote["pre"])
        if not (isfinite(price) and isfinite(previous) and price > 0 and previous > 0):
            return None
        stamp = datetime.strptime(quote["date"] + values[0], "%Y%m%d%H%M").replace(tzinfo=SH)
        return {
            "board_name": board["name"],
            "kind": board["kind"],
            "source": "ths",
            "code": quote_code,
            "catalog_code": code,
            "source_at": stamp,
            "pct_chg": (price / previous - 1) * 100,
        }
    return None


def fetch_ths(as_of, read):
    boards = ths_shortlist(as_of)
    if not boards:
        return pd.DataFrame()

    def fetch(board):
        try:
            return ths_minutes(board, read)
        except (OSError, HTTPException, ValueError, TypeError, KeyError) as exc:
            log.info("同花顺分时未取得 %s: %s", board["name"], type(exc).__name__)
            return None

    with ThreadPoolExecutor(max_workers=4) as workers:
        return pd.DataFrame(row for row in workers.map(usage.wrap(fetch), boards) if row)


def save(frame, captured_at):
    if frame.empty:
        return
    rows = []
    for row in frame.astype(object).where(frame.notna(), None).to_dict("records"):
        if not row.get("source") or not row.get("code"):
            continue

        rows.append(
            (
                captured_at,
                row["source"],
                row["code"],
                row.get("catalog_code") or row["code"],
                row["board_name"],
                row["kind"],
                row.get("source_at"),
                row["pct_chg"],
                row.get("main_net"),
                row.get("main_ratio"),
            )
        )
    if rows:
        db.execute_many(
            f"INSERT INTO {db.qualified('board_spot_observation')} "
            "(captured_at,source,source_code,catalog_code,board_name,kind,source_at,pct_chg,main_net,main_ratio) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            rows,
        )


def available(as_of):
    cutoff = as_of.astimezone(SH)
    if not (time(9, 30) <= cutoff.time() <= time(11, 30) or time(13) <= cutoff.time() <= time(15)):
        return {}
    rows = db.query(
        f"""SELECT DISTINCT ON (b.board_id) b.board_id,s.captured_at,s.source_at,s.source,s.pct_chg,
                  s.source_code,s.catalog_code,s.main_net,s.main_ratio
            FROM {db.qualified("board_spot_observation")} s JOIN {db.qualified("board")} b
              ON b.code=s.catalog_code AND b.source=s.source AND b.kind=s.kind AND b.is_active
            JOIN {db.qualified("trade_calendar")} c ON c.trade_date=%s
            WHERE s.captured_at BETWEEN %s AND %s AND s.source_at BETWEEN %s AND s.captured_at
              AND s.source IN ('ths','eastmoney')
            ORDER BY b.board_id,s.source_at DESC,s.captured_at DESC""",
        [
            cutoff.date(),
            cutoff - timedelta(minutes=30),
            cutoff,
            max(
                datetime.combine(
                    cutoff.date(), time(13) if cutoff.time() >= time(13) else time(9, 30), SH
                ),
                cutoff - timedelta(minutes=30),
            ),
        ],
    )
    return {r["board_id"]: r for r in rows}
