from contextlib import nullcontext
from datetime import date

import pandas as pd
import pytest

from quantlab import ingest, market


def test_flow_rank_requires_same_day_closes_and_writes_yuan_and_percentage_points(monkeypatch):
    close_day = date(2026, 11, 20)
    monkeypatch.setattr(ingest, "_latest_closed_session", lambda: close_day)
    monkeypatch.setattr(ingest, "phase", lambda _phase: nullcontext({}))
    anchors = pd.DataFrame(
        {
            "board_id": [1, 2, 3],
            "name": list("甲乙丙"),
            "reference_close": [101.0, 202.0, 303.0],
            "reference_pct": [1, 2, 3],
        }
    )
    rank = pd.DataFrame(
        {
            "name": list("甲乙丙"),
            "snapshot_close": [102.0, 204.0, 306.0],
            "inflow": [6e8, 4e8, 5e8],
            "outflow": [4e8, 6e8, 5e8],
            "main_net": [2e8, -2e8, 0.0],
        }
    )
    monkeypatch.setattr(ingest.db, "query_df", lambda *_: anchors)
    monkeypatch.setattr(ingest.sources, "fetch_board_flow_ths", lambda *_: rank.copy())
    written = []

    def save(_sql, rows):
        written.extend(rows)
        return len(written)

    monkeypatch.setattr(ingest.db, "execute_many", save)
    assert ingest.update_board_flow() == 0 and not written
    rank["snapshot_close"] = anchors["reference_close"]
    assert ingest.update_board_flow() == 3
    assert [(r[1], r[3], r[4], r[-1]) for r in written] == [
        (close_day, 2e8, 20.0, "ths-close-verified"),
        (close_day, -2e8, -20.0, "ths-close-verified"),
        (close_day, 0.0, 0.0, "ths-close-verified"),
    ]
    with pytest.raises(ValueError):
        ingest.update_board_flow(trade_date=date(2026, 11, 21))


def test_current_stale_and_undated_observations_have_distinct_evidence():
    day = date(2026, 11, 20)
    old = date(2026, 11, 19)

    def read(source, observed, value):
        return market.flow_evidence(source, "industry", observed, value, None, day)

    assert read("ths-close-verified", day, 0)["flow_status"] == "available"
    assert read("ths-close-verified", old, 100)["flow_status"] == "stale"
    assert read("ths", day, 100)["flow_status"] == "unverified"
    assert read(None, None, None)["net_yuan"] is None
