"""初始化数据库：应用 schema + 用 configs/universe.yaml 播种基金主数据。

幂等：重复执行只更新，不会产生重复行。

    docker compose run --rm scheduler python scripts/init_db.py
    docker compose run --rm scheduler python scripts/init_db.py --counts
"""

from __future__ import annotations

import argparse

from quantlab import db
from quantlab.config import load_universe
from quantlab.fund_universe import seed_disclosed_redemption_fees

INSTRUMENT_SQL = f"""
INSERT INTO {db.qualified("instrument")}
    (code, name, kind, share_class, role, track_index, is_active)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (code) DO UPDATE SET
    name        = EXCLUDED.name,
    kind        = EXCLUDED.kind,
    share_class = EXCLUDED.share_class,
    role        = EXCLUDED.role,
    track_index = EXCLUDED.track_index,
    is_active   = EXCLUDED.is_active,
    updated_at  = now()
"""


def seed_instruments(universe: dict) -> None:
    assets = universe["assets"]
    fee_table = universe.get("fees", {})

    instrument_rows = [
        (
            asset["code"],
            asset["name"],
            asset.get("kind", "otc"),
            asset.get("share_class"),
            asset.get("role", "risk"),
            asset.get("track_index"),
            asset.get("is_active", True),
        )
        for asset in assets
    ]
    db.execute_many(INSTRUMENT_SQL, instrument_rows)

    code_to_id = {
        row["code"]: row["instrument_id"]
        for row in db.query(f"SELECT instrument_id, code FROM {db.qualified('instrument')}")
    }

    fee_rows: list[tuple] = []
    checked = seed_disclosed_redemption_fees(universe.get("disclosed_redemption_fees", {}))
    for asset in assets:
        if asset["code"] in checked:
            continue
        schedule = fee_table.get(asset.get("share_class") or "", {})
        if not schedule or asset["code"] not in code_to_id:
            continue
        instrument_id = code_to_id[asset["code"]]
        fee_rows.append((instrument_id, "buy", 999_999, float(schedule.get("buy", 0.0))))
        for limit, rate in schedule.get("sell", []):
            fee_rows.append((instrument_id, "sell", int(limit), float(rate)))

    if fee_rows:
        db.upsert(
            "instrument_fee",
            ["instrument_id", "side", "holding_days_max", "rate"],
            fee_rows,
            conflict=["instrument_id", "side", "holding_days_max"],
            update=["rate"],
        )

    # 板块暴露（可选）：universe.yaml 里给每只基金写 boards: [板块名称]
    board_lookup = {
        row["name"]: row["board_id"]
        for row in db.query(f"SELECT board_id, name FROM {db.qualified('board')}")
    }
    board_rows: list[tuple] = []
    unmatched: set[str] = set()
    for asset in assets:
        instrument_id = code_to_id.get(asset["code"])
        for board_name in asset.get("boards") or []:
            if instrument_id is None:
                break
            board_id = board_lookup.get(board_name)
            if board_id is None:
                unmatched.add(board_name)
                continue
            board_rows.append((instrument_id, board_id, 1.0))

    if board_rows:
        db.upsert(
            "instrument_board",
            ["instrument_id", "board_id", "weight"],
            board_rows,
            conflict=["instrument_id", "board_id"],
            update=["weight", "updated_at"],
        )
    if unmatched:
        print("未匹配到板块（先跑 ingest_market 抓板块清单）：", ", ".join(sorted(unmatched)))


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 QuantLab 数据库")
    parser.add_argument("--counts", action="store_true", help="只打印各表行数")
    args = parser.parse_args()

    db.wait_for_db()
    if not args.counts:
        db.apply_schema()
        seed_instruments(load_universe())
        print("schema 与基金主数据已就绪")

    print("\n各表行数：")
    for table, count in db.table_counts().items():
        print(f"  {table:<20} {count}")


if __name__ == "__main__":
    main()
