from contextlib import nullcontext
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from quantlab import replay_verification as verification

BASE = date(2026, 10, 27)
FIRST = date(2026, 10, 28)
SECOND = date(2026, 11, 3)


def forecast(ident=1, target=FIRST):
    return {
        "forecast_id": ident,
        "board_id": 94,
        "board": "AI语料",
        "name": "AI语料",
        "code": "309126",
        "base_date": BASE,
        "target_date": target,
        "prob_up": 0.36,
        "kind": "concept",
        "source": "ths",
        "original_base_close": Decimal("1145.1640"),
        "original_target_close": None,
        "base_source": "ths",
        "target_source": None,
        "proxy_code": "",
    }


@pytest.mark.parametrize(
    "values",
    [
        [(FIRST, "1145.164"), (SECOND, "1190")],  # missing base session
        [(BASE, "1145.165"), (FIRST, "1190")],  # disagreement with frozen baseline
        [(BASE, "1145.164"), (FIRST, "1190"), (FIRST, "1191")],
        [(BASE, "1145.164"), (FIRST, "NaN")],
    ],
)
def test_incomplete_or_disagreeing_quotes_are_never_scored(values):
    frame = pd.DataFrame(values, columns=["trade_date", "close"])
    with pytest.raises(ValueError):
        verification._pair(forecast(), verification._prices(frame, {BASE, FIRST}))


def test_only_one_historical_series_fetch_for_multiple_due_dates(monkeypatch):
    monkeypatch.setattr(verification.db, "transaction", nullcontext)
    monkeypatch.setattr(verification, "_database", lambda: "quantlab_replay_test")
    monkeypatch.setattr(verification.db, "query_one", lambda _: {"acquired": True})
    monkeypatch.setattr(verification, "_finished_account", lambda _: (1, BASE, SECOND))
    monkeypatch.setattr(verification, "_create_table", lambda: None)
    rows = [forecast(), forecast(2, SECOND), {**forecast(3), "proxy_code": "000001"}]
    monkeypatch.setattr(verification, "_missing", lambda *_: rows)
    monkeypatch.setattr(verification.sources, "fetch_boards_ths", lambda _: pd.DataFrame([rows[0]]))
    calls = []

    def history(*args):
        calls.append(args)
        return pd.DataFrame(
            [(BASE, "1145.164"), (FIRST, "1190"), (SECOND, "1110")], columns=["trade_date", "close"]
        )

    monkeypatch.setattr(verification.sources, "fetch_board_hist_ths", history)
    writes = []

    def append(sql, params):
        assert "ON CONFLICT (forecast_id) DO NOTHING" in sql
        writes.append(params)
        return 1

    monkeypatch.setattr(verification.db, "execute", append)
    result = verification.verify_missing()
    assert calls == [("AI语料", "concept", "20261027", "20261103")]
    assert [r[10] for r in writes] == [False, True]
    assert result["inserted"] == 2
    assert [r["forecast_id"] for r in result["unresolved"]] == [3]


def test_incomplete_and_live_account_cannot_create_verification_table(monkeypatch):
    monkeypatch.setattr(verification.db, "transaction", nullcontext)
    monkeypatch.setattr(verification.db, "execute", lambda *_: pytest.fail("unexpected write"))
    monkeypatch.setattr(verification.db, "query_one", lambda *_: {"name": "quantlab"})
    with pytest.raises(ValueError, match="独立回放"):
        verification.verify_missing()
    manifest = {
        "mode": "historical_replay",
        "run_id": "quantlab_replay_test",
        "start": str(BASE),
        "end": str(SECOND),
    }
    monkeypatch.setattr(
        verification.db,
        "query",
        lambda *_: [{"pod_id": 1, "strategy_key": "agent_manager", "entry_evidence": manifest}],
    )
    monkeypatch.setattr(
        verification.db,
        "query_one",
        lambda *_: {"closed": True, "sessions": 6, "settlements": 5, "valuations": 5},
    )
    with pytest.raises(ValueError, match="未完成"):
        verification._finished_account("quantlab_replay_test")
