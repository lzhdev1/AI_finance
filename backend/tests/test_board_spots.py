import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pandas as pd
import pytest

from quantlab import board_spots, db, realtime, research
from quantlab.config import DB_DIR

NOW = datetime(2026, 11, 20, 14, 30, tzinfo=board_spots.SH)


def test_source_code_and_minute_are_from_same_vendor_series():
    board = {"code": "300008", "kind": "concept", "name": "新能源汽车"}
    quote = {
        "name": board["name"],
        "date": "20261120",
        "pre": "100",
        "data": "1428,100.2,0;1429,100.6,0;",
    }

    def read(url, **kwargs):
        if "detail/code/300008/" in url:
            return '<input id="clid" value="885431" />'
        assert url.endswith("/bk_885431/last.js")
        return "quotebridge_v4_time_bk_885431_last(" + json.dumps({"bk_885431": quote}) + ")"

    row = board_spots.ths_minutes(board, read)
    assert row["source"] == "ths" and row["catalog_code"] == "300008" and row["code"] == "885431"
    assert row["source_at"] == NOW - timedelta(minutes=1)
    assert row["pct_chg"] == pytest.approx(0.6)
    assert "main_net" not in row
    quote["name"] = "另一个板块"
    assert board_spots.ths_minutes(board, read) is None


def test_eastmoney_captures_epoch_field_and_missing_clock_stays_missing(monkeypatch):
    def get(url, **kwargs):
        assert "f124" in url
        return json.dumps(
            {
                "data": {
                    "diff": [
                        {
                            "f12": "BK1",
                            "f14": "同名",
                            "f3": 0.2,
                            "f62": 2000,
                            "f124": int(NOW.timestamp()),
                        },
                        {"f12": "BK2", "f14": "同名", "f3": 3, "f124": "-"},
                    ]
                }
            }
        )

    monkeypatch.setattr(realtime, "_get", get)
    rows = realtime.fetch_board_spot()
    assert list(rows["code"]) == ["BK1", "BK2"]
    assert rows.iloc[0]["source"] == "eastmoney" and rows.iloc[0]["source_at"] == NOW
    assert pd.isna(rows.iloc[1]["source_at"])


@pytest.mark.skipif(os.getenv("QUANTLAB_TEST_POSTGRES") != "1", reason="requires PostgreSQL")
def test_only_verified_live_observations_reach_board_research(monkeypatch):
    schema = "spot_test_" + uuid4().hex
    monkeypatch.setattr(db, "_schema", schema)
    with db.transaction() as conn:
        for filename in ("schema.sql", "agent_research.sql"):
            conn.execute(
                (DB_DIR / filename).read_text(encoding="utf-8").replace("quantlab", schema)
            )
        t = db.qualified
        db.execute(f"INSERT INTO {t('trade_calendar')}(trade_date) VALUES (%s)", [NOW.date()])
        for bid, source, code in [
            (1, "ths", "881121"),
            (2, "eastmoney", "BK1"),
            (3, "ths", "881122"),
        ]:
            db.execute(
                f"INSERT INTO {t('board')}(board_id,code,name,kind,source) VALUES (%s,%s,'同名','industry',%s)",
                [bid, code, source],
            )
        # Keep original name-based snapshots independently reproducible.
        db.execute(
            f"INSERT INTO {t('board_intraday')}(ts,board_name,pct_chg) VALUES (%s,'同名',90)", [NOW]
        )
        row = {
            "code": "881121",
            "board_name": "同名",
            "kind": "industry",
            "source": "ths",
            "source_at": NOW - timedelta(minutes=3),
            "pct_chg": 1,
        }
        board_spots.save(pd.DataFrame([row]), NOW)
        em = row | {"code": "BK1", "source": "eastmoney", "pct_chg": 2, "main_net": 123.0}
        board_spots.save(pd.DataFrame([em]), NOW)
        for i, data in enumerate(
            [
                {"source_at": NOW + timedelta(minutes=1)},
                {"source_at": None},
                {"source_at": NOW - timedelta(minutes=31)},
                {"source_at": NOW - timedelta(days=1)},
                {"source_at": NOW - timedelta(minutes=1), "code": "BK_wrong"},
                {"source_at": NOW - timedelta(minutes=1), "kind": "concept"},
                {"source_at": NOW - timedelta(minutes=1), "source": "sina"},
            ]
        ):
            board_spots.save(
                pd.DataFrame([em | data | {"pct_chg": 80}]), NOW - timedelta(seconds=i + 1)
            )
        board_spots.save(
            pd.DataFrame([em | {"source_at": NOW, "pct_chg": 99}]), NOW + timedelta(seconds=1)
        )
        live = research._intraday(NOW, NOW.date(), include_flow=True)
        assert set(live) == {1, 2} and live[1]["return"] == 0.01 and live[2]["return"] == 0.02
        assert "capital_flow" not in live[1] and live[2]["capital_flow"]["net_yuan"] == 123
        assert live[1]["at"] != live[1]["captured_at"]
        assert research._intraday(NOW, NOW.date())["同名"]["return"] == 0.9
        assert not board_spots.available(NOW + timedelta(days=1))
        assert board_spots.ths_shortlist(NOW)[0]["name"] == "同名"
        conn.rollback()
