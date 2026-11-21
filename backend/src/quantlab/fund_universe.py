"""把板块链接到可投基金（instrument_board）。

做法不神秘：**基金名称里带板块名的场外 C 类指数基金**。
例如板块「半导体」→「华夏国证半导体芯片ETF联接C」。

这是启发式匹配，不是权威映射，所以：
* 只取每个板块 1 只（优先带"指数/联接/ETF"字样的 C 类）；
* 结果标记 source='name-match'，方便以后换成人工维护或相关性验证；
* 匹配不到就留空，宁可没映射，也不要塞一只不相关的基金进去。
"""

from __future__ import annotations

import logging
import os
import re

from quantlab import db, sources
from quantlab.config import load_universe

log = logging.getLogger(__name__)

GOOD_TYPES = ("指数型-股票", "股票型", "指数型-其他")
INDEX_HINTS = ("指数", "联接", "ETF", "LOF")
GENERIC_NAMES = {"综合", "其他", "其它", "非银", "金融服务"}


def clean_name(text: str) -> str:
    return re.sub(r"\s+", "", str(text))


def seed_disclosed_redemption_fees(schedules: dict) -> dict[str, int]:
    """Install a whole missing sell schedule only in the configured online simulation database."""
    if not schedules:
        return {}
    with db.transaction():
        database = db.query_one("SELECT current_database() AS name")["name"]
        if database.startswith("quantlab_replay_") or database != os.getenv("POSTGRES_DB", "quantlab"):
            return {}
        instruments = db.query(
            f"SELECT instrument_id,code,share_class FROM {db.qualified('instrument')} "
            "WHERE kind='otc' AND code=ANY(%s) ORDER BY instrument_id FOR UPDATE",
            [list(schedules)],
        )
        counts = {}
        fee_table = db.qualified("instrument_fee")
        for fund in instruments:
            code = fund["code"]
            policy = schedules[code]
            if fund["share_class"] != policy["share_class"]:
                continue
            instrument = fund["instrument_id"]
            counts[code] = 0
            if db.query_one(
                f"SELECT 1 FROM {fee_table} WHERE instrument_id=%s AND side='sell' LIMIT 1",
                [instrument],
            ):
                continue
            counts[code] = db.execute_many(
                f"INSERT INTO {fee_table} (instrument_id, side, holding_days_max, rate) "
                "VALUES (%s,'sell',%s,%s)",
                [(instrument, days, rate) for days, rate in policy["sell"]],
            )
        return counts


def pick_fund(board_name: str, frame) -> str | None:
    if len(board_name.strip()) < 2 or board_name in GENERIC_NAMES:
        return None
    hits = frame[frame["简称"].str.contains(board_name, regex=False, na=False)]
    if hits.empty:
        return None
    # 优先：C 类 + 名称里明确是指数/联接
    scored = []
    for row in hits.itertuples():
        name = row.简称
        score = 0
        if name.rstrip().endswith("C"):
            score += 3
        if any(hint in name for hint in INDEX_HINTS):
            score += 2
        if row.类型 in ("指数型-股票", "股票型"):
            score += 1
        # 名称越短越可能是"纯"行业基金
        score -= len(name) / 100.0
        scored.append((score, row.代码, name))
    scored.sort(reverse=True)
    return f"{scored[0][1]}|{scored[0][2]}"


def link_funds(kind="industry", limit=0, dry_run=False):
    log.info("拉取全市场基金名单…")
    frame = sources.fetch_fund_list()
    frame["name"] = frame["name"].map(clean_name)
    frame = frame[
        frame["fund_type"].isin(GOOD_TYPES)
        & frame["name"].str.endswith("C")
        & frame["name"].str.contains("指数|联接|ETF", regex=True)
    ]
    frame["简称"] = frame["name"]
    frame["类型"] = frame["fund_type"]
    frame["代码"] = frame["code"]
    log.info("候选基金 %s 只（已按类型过滤）", len(frame))

    boards = db.query(
        f"SELECT board_id, name, kind FROM {db.qualified('board')} "
        "WHERE kind = %s AND is_active ORDER BY board_id",
        [kind],
    )
    if limit:
        boards = boards[:limit]

    matched = 0
    schedules = load_universe().get("disclosed_redemption_fees", {})
    for board in boards:
        picked = pick_fund(board["name"], frame)
        if not picked:
            continue
        code, name = picked.split("|", 1)
        matched += 1
        print(f"  {board['name']:<12} → {code} {name}")
        if dry_run:
            continue
        db.execute(
            f"""
            INSERT INTO {db.qualified("instrument")} (code, name, kind, share_class, role, is_active)
            VALUES (%s, %s, 'otc', 'C', 'risk', true)
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
            """,
            [code, name],
        )
        row = db.query_one(
            f"SELECT instrument_id FROM {db.qualified('instrument')} WHERE code = %s", [code]
        )
        checked = seed_disclosed_redemption_fees({code: schedules[code]}) if code in schedules else {}
        if code not in checked:
            instrument_id = row["instrument_id"]
            db.execute_many(
                f"INSERT INTO {db.qualified('instrument_fee')} (instrument_id,side,holding_days_max,rate) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(instrument_id, "buy", 999999, 0.0), (instrument_id, "sell", 7, 0.015),
                 (instrument_id, "sell", 999999, 0.0)],
            )
        db.execute(
            f"""
            INSERT INTO {db.qualified("instrument_board")} (instrument_id, board_id, weight, source)
            VALUES (%s, %s, 1.0, 'name-match')
            ON CONFLICT (instrument_id, board_id) DO UPDATE SET weight = 1.0, updated_at = now()
            """,
            [row["instrument_id"], board["board_id"]],
        )

    print(f"\n共匹配 {matched}/{len(boards)} 个板块")
    if not dry_run:
        total = db.query_one(f"SELECT count(*) AS n FROM {db.qualified('instrument_board')}")
        print(f"instrument_board 现有 {total['n']} 条映射")

    return matched
