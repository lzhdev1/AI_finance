"""Append independently retrieved index outcomes after a historical paper account has finished."""

import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from . import db, sources


def _database():
    name = db.query_one("SELECT current_database() AS name")["name"]
    if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", name):
        raise ValueError("期后复核仅允许在独立回放数据库运行")
    return name


def _finished_account(database):
    t = db.qualified
    accounts = db.query(f"SELECT pod_id, strategy_key, entry_evidence FROM {t('pod')}")
    if len(accounts) != 1 or accounts[0]["strategy_key"] != "agent_manager":
        raise ValueError("期后复核需要唯一的历史经理账户")
    pod = accounts[0]
    manifest = pod["entry_evidence"] or {}
    if manifest.get("mode") != "historical_replay" or manifest.get("run_id") != database:
        raise ValueError("独立回放账户标识不符")
    start, end = (date.fromisoformat(manifest[k]) for k in ("start", "end"))
    coverage = db.query_one(
        f"""
        SELECT count(*) AS sessions, count(n.trade_date) AS valuations, count(d.executed_at) AS settlements,
               %s::date < (now() AT TIME ZONE 'Asia/Shanghai')::date AS closed
        FROM {t("trade_calendar")} c
        LEFT JOIN {t("pod_nav")} n ON n.pod_id=%s AND n.trade_date=c.trade_date
        LEFT JOIN {t("pod_decision")} d ON d.pod_id=%s AND d.trade_date=c.trade_date
        WHERE c.trade_date BETWEEN %s AND %s""",
        [end, pod["pod_id"], pod["pod_id"], start, end],
    )
    if (
        not coverage["closed"]
        or not coverage["sessions"]
        or not (coverage["sessions"] == coverage["valuations"] == coverage["settlements"])
    ):
        raise ValueError("账户仍有交易日未完成回放")
    return pod["pod_id"], start, end


def _create_table():
    t = db.qualified
    db.execute(f"""CREATE TABLE IF NOT EXISTS {t("replay_forecast_verification")} (
        forecast_id bigint PRIMARY KEY REFERENCES {t("agent_board_forecast")}(forecast_id),
        board_id integer NOT NULL REFERENCES {t("board")}(board_id),
        board_code text NOT NULL, board_name text NOT NULL,
        base_date date NOT NULL, target_date date NOT NULL CHECK (target_date > base_date),
        base_close numeric NOT NULL CHECK (base_close > 0), target_close numeric NOT NULL CHECK (target_close > 0),
        original_base_close numeric CHECK (original_base_close = base_close),
        original_target_close numeric CHECK (original_target_close = target_close),
        realized_return numeric GENERATED ALWAYS AS (target_close/base_close - 1) STORED,
        hit boolean, source text NOT NULL CHECK (source = 'ths'), source_url text NOT NULL,
        verified_at timestamptz NOT NULL DEFAULT clock_timestamp()
    )""")


def _missing(pod_id, start, end):
    t = db.qualified
    return db.query(
        f"""
        SELECT f.forecast_id, f.board_id, f.board, f.base_date, f.target_date, f.prob_up, f.proxy_code,
               b.name, b.code, b.kind, b.source,
               a.close AS original_base_close, a.source AS base_source,
               z.close AS original_target_close, z.source AS target_source
        FROM {t("agent_board_forecast")} f JOIN {t("board")} b USING(board_id)
        LEFT JOIN {t("board_quote")} a ON a.board_id=f.board_id AND a.trade_date=f.base_date
        LEFT JOIN {t("board_quote")} z ON z.board_id=f.board_id AND z.trade_date=f.target_date
        WHERE f.scored_at IS NULL AND f.realized_return IS NULL AND f.hit IS NULL AND f.target_date <= %s
          AND NOT EXISTS (SELECT FROM {t("replay_forecast_verification")} v WHERE v.forecast_id=f.forecast_id)
          AND EXISTS (
            SELECT FROM {t("agent_report_forecast")} l
            JOIN {t("pod_decision")} d ON (d.payload->>'research_id')::bigint=l.report_id
            WHERE l.forecast_id=f.forecast_id AND d.pod_id=%s AND d.executed_at IS NOT NULL
              AND d.trade_date BETWEEN %s AND %s)
        ORDER BY f.board_id,f.forecast_id""",
        [end, pod_id, start, end],
    )


def _prices(frame, days):
    prices = defaultdict(set)
    for r in frame.itertuples():
        if r.trade_date in days:
            try:
                value = Decimal(str(r.close))
            except InvalidOperation:
                value = None
            prices[r.trade_date].add(
                value if value is not None and value.is_finite() and value > 0 else None
            )
    return prices


def _pair(row, prices):
    closes = []
    for end in ("base", "target"):
        day = row[f"{end}_date"]
        values = prices.get(day, set())
        if len(values) != 1 or None in values:
            raise ValueError(f"{day} 缺少唯一有效的实际收盘")
        close = next(iter(values))
        frozen = row[f"original_{end}_close"]
        if frozen is not None and (row[f"{end}_source"] != "ths" or frozen != close):
            raise ValueError(f"{day} 原始收盘或来源与本次查询不一致")
        closes.append(close)
    return closes


def _record(row, prices):
    base, target = _pair(row, prices)
    segment = {"concept": "gn", "industry": "thshy"}[row["kind"]]
    url = f"https://q.10jqka.com.cn/{segment}/detail/code/{row['code']}/"
    probability = row["prob_up"]
    hit = None if probability == 0.5 else (probability > 0.5) == (target > base)
    return db.execute(
        f"""INSERT INTO {db.qualified("replay_forecast_verification")}
        (forecast_id, board_id, board_name, board_code, base_date, target_date, base_close, target_close,
         original_base_close, original_target_close, hit, source, source_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ths',%s)
        ON CONFLICT (forecast_id) DO NOTHING""",
        [
            row["forecast_id"],
            row["board_id"],
            row["name"],
            row["code"],
            row["base_date"],
            row["target_date"],
            base,
            target,
            row["original_base_close"],
            row["original_target_close"],
            hit,
            url,
        ],
    )


def verify_missing():
    """Verify exact dates in one historical series request per board. Retry skips recorded rows."""
    with db.transaction():
        database = _database()
        # Share the existing runner's lock so a replay cannot advance while evidence is appended.
        if not db.query_one("SELECT pg_try_advisory_xact_lock(794022005) AS acquired")["acquired"]:
            raise RuntimeError("回放或期后复核仍在进行")
        pod_id, start, end = _finished_account(database)
        _create_table()
        rows = _missing(pod_id, start, end)
        result = {"eligible": len(rows), "requested_boards": 0, "inserted": 0, "unresolved": []}
        groups = defaultdict(list)
        for row in rows:
            if (
                row["proxy_code"]
                or row["source"] != "ths"
                or row["board"] != row["name"]
                or row["kind"] not in {"concept", "industry"}
            ):
                result["unresolved"].append(
                    {"forecast_id": row["forecast_id"], "reason": "无法确认原同花顺板块序列"}
                )
            else:
                groups[(row["kind"], row["name"], row["code"])].append(row)
        directories = {}
        for (kind, name, code), group in groups.items():
            try:
                if kind not in directories:
                    directory = sources.fetch_boards_ths(kind)
                    directories[kind] = {(r.name, str(r.code)) for r in directory.itertuples()}
                if (name, code) not in directories[kind]:
                    raise ValueError("同花顺当前目录与冻结的板块名称、代码不一致")
                days = {day for r in group for day in (r["base_date"], r["target_date"])}
                result["requested_boards"] += 1
                frame = sources.fetch_board_hist_ths(
                    name, kind, min(days).strftime("%Y%m%d"), max(days).strftime("%Y%m%d")
                )
                prices = _prices(frame, days)
            except Exception as exc:  # noqa: BLE001 - this provider failure must not discard other boards.
                result["unresolved"].extend(
                    {"forecast_id": r["forecast_id"], "reason": str(exc)} for r in group
                )
                continue
            for row in group:
                try:
                    result["inserted"] += _record(row, prices)
                except ValueError as exc:
                    result["unresolved"].append(
                        {"forecast_id": row["forecast_id"], "reason": str(exc)}
                    )
        return result


def list_verifications():
    """Read supplementary evidence without creating an empty table in a new replay."""
    _database()
    table = db.qualified("replay_forecast_verification")
    if not db.query_one("SELECT to_regclass(%s) IS NOT NULL AS present", [table])["present"]:
        return []
    return db.query(f"SELECT * FROM {table} ORDER BY forecast_id")


if __name__ == "__main__":
    status = verify_missing()
    print(json.dumps(status, ensure_ascii=False))
    raise SystemExit(1 if status["unresolved"] else 0)
