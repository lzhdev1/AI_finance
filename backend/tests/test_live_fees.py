from contextlib import nullcontext

import pytest

from quantlab import fund_universe
from quantlab.config import load_universe
from quantlab.costs import holding_rate


def test_disclosed_fees_seed_missing_schedule_once_and_preserve_existing(monkeypatch):
    db = fund_universe.db
    schedules = load_universe()["disclosed_redemption_fees"]
    monkeypatch.setenv("POSTGRES_DB", "quantlab")
    monkeypatch.setattr(db, "transaction", nullcontext)
    rows = []
    fund = {"code": "008089", "share_class": "C", "instrument_id": 22}
    monkeypatch.setattr(db, "query", lambda *args: [fund])
    monkeypatch.setattr(
        db,
        "query_one",
        lambda sql, *args: {"name": "quantlab"} if "current_database" in sql else bool(rows),
    )

    def insert(sql, values):
        assert "'sell'" in sql
        rows.extend(values)
        return len(values)

    monkeypatch.setattr(db, "execute_many", insert)
    assert fund_universe.seed_disclosed_redemption_fees(schedules) == {"008089": 3}
    assert [holding_rate([(n, r) for _, n, r in rows], d) for d in [6, 7, 29, 30]] == [
        0.015,
        0.005,
        0.005,
        0,
    ]
    assert fund_universe.seed_disclosed_redemption_fees(schedules) == {"008089": 0}
    rows[:] = [(22, 15, 0.004)]
    assert fund_universe.seed_disclosed_redemption_fees(schedules) == {"008089": 0}
    assert rows == [(22, 15, 0.004)]


@pytest.mark.parametrize(
    "database", ["quantlab_replay_202611", "quantlab_replay_202611_v2", "other"]
)
def test_seeding_rejects_replay_and_other_databases_before_writing(monkeypatch, database):
    db = fund_universe.db
    monkeypatch.setenv("POSTGRES_DB", "quantlab")
    monkeypatch.setattr(db, "transaction", nullcontext)
    monkeypatch.setattr(db, "query_one", lambda *_: {"name": database})
    monkeypatch.setattr(db, "query", lambda *_: pytest.fail("unexpected instrument access"))
    assert (
        fund_universe.seed_disclosed_redemption_fees(load_universe()["disclosed_redemption_fees"])
        == {}
    )
