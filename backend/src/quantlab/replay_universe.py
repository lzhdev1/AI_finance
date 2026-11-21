"""Effective-dated disclosures for the historical manager. The live universe is never changed."""

from __future__ import annotations

import re
from datetime import date

from . import db

BATTERY_CHANGE = "https://pdf.dfcfw.com/pdf/H2_AN202609191829639183_1.pdf"
GRID_CHANGE = "https://pdf.dfcfw.com/pdf/H2_AN202611191831843421_1.pdf"
SOFTWARE_OPENING = "https://pdf.dfcfw.com/pdf/H2_AN202608251828386795_1.pdf"


def terms_on(day: date) -> list[dict]:
    """Only point-in-time changes that affect a name, benchmark methodology or trading eligibility."""
    battery_renamed = day >= date(2026, 9, 21)
    grid_converted = day >= date(2026, 11, 20)
    return [
        {
            "code": "021964",
            "name": "天弘国证储能电池指数发起C" if battery_renamed else "天弘国证新能源电池指数发起C",
            "is_active": True,
            "evidence": BATTERY_CHANGE if battery_renamed else "https://pdf.dfcfw.com/pdf/H2_AN202506271698410152_1.pdf",
        },
        {
            "code": "025833",
            "name": "天弘中证电网设备主题ETF发起联接C" if grid_converted else "天弘中证电网设备主题指数发起C",
            "is_active": True,
            "evidence": GRID_CHANGE if grid_converted else "https://pdf.dfcfw.com/pdf/H2_AN202512161801486480_1.pdf",
        },
        {
            "code": "028521",
            "name": "前海开源中证全指软件开发指数C",
            "is_active": day >= date(2026, 8, 27),
            "evidence": SOFTWARE_OPENING if day >= date(2026, 8, 25) else None,
        },
    ]


def apply_as_of(day: date) -> list[dict]:
    """Apply to an isolated replay transaction before assembling the day's research context."""
    with db.transaction():
        current = db.query_one("SELECT current_database() AS name")["name"]
        if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", current):
            raise RuntimeError("回放资料只可应用于专用 quantlab_replay_* 数据库")
        updates = terms_on(day)
        db.execute_many(
            f"UPDATE {db.qualified('instrument')} SET name=%s,is_active=%s WHERE code=%s "
            "AND (name,is_active) IS DISTINCT FROM (%s,%s)",
            [(r["name"], r["is_active"], r["code"], r["name"], r["is_active"]) for r in updates],
        )
        return updates
