from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from quantlab import opportunity
from quantlab.opportunity_evaluation import (
    Ledger,
    SH,
    _forward_checks,
    _performance,
    _session_inputs,
    _timing,
)
from quantlab.pods import PodAccount


def test_monthly_performance_starts_at_previous_close_and_counts_only_fills_in_month():
    rows = [
        {"date": "2026-05-29", "value": 200, "invested": 100, "orders": []},
        {
            "date": "2026-06-01",
            "value": 75,
            "invested": 25,
            "orders": [{"side": "sell", "fee": 1.75}],
        },
        {"date": "2026-06-02", "value": 90, "invested": 0, "orders": []},
    ]
    metrics = _performance(rows, 200, "2026-06-01", "2026-06-02")
    assert metrics == {
        "return": pytest.approx(-0.55),
        "max_drawdown": -0.625,
        "fees": 1.75,
        "buy_count": 0,
        "sell_count": 1,
        "exposure_days": 1,
    }


def test_forward_checks_use_exact_target_session_and_exclude_unknown_outcomes():
    days = [
        date(2026, 5, 28),
        date(2026, 5, 29),
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
        date(2026, 6, 4),
    ]
    navs = {days[1]: {"A": 0.9}, days[2]: {"A": 1.2}, days[5]: {"A": 1.1}}
    buys = [
        {"date": str(days[0]), "code": "A", "nav": 1},
        {"date": str(days[-1]), "code": "A", "nav": 1.1},
    ]
    one, five = _forward_checks(buys, days, navs, days[-1])
    assert (one["hits"], one["misses"], one["unresolved"]) == (0, 1, 1)
    assert (five["hits"], five["misses"], five["unresolved"]) == (1, 0, 1)
    assert buys[0]["return_1d"] == pytest.approx(-0.1)
    assert buys[0]["return_5d"] == pytest.approx(0.1)
    assert buys[-1]["return_1d"] is None


def test_simulated_shares_and_bank_proceeds_unlock_on_their_respective_sessions():
    sessions = [date(2026, 6, 1) + timedelta(days=i) for i in (0, 1, 2, 3, 4, 7, 8, 9, 10)]
    account = PodAccount(10000, 10000)
    bought = account.buy(sessions[0], "A", 1000, 1, "test")
    ledger = Ledger(account, withdrawals=[(8, 500)])
    assert _timing(ledger, sessions, 1) == {"redeemable": {"A": 0}, "cash_receivable": 500}
    assert _timing(ledger, sessions, 2)["redeemable"] == {"A": bought.shares}
    assert _timing(ledger, sessions, 7)["cash_receivable"] == 500
    assert _timing(ledger, sessions, 8)["cash_receivable"] == 0


def test_observer_receives_no_current_or_future_prices_or_funds_without_a_visible_disclosure(
    monkeypatch,
):
    sessions = [date(2026, 6, 1) + timedelta(days=i) for i in (0, 1, 2, 3, 4, 7, 8, 9, 10)]
    decision = sessions[2]
    report = {"day": decision, "as_of": datetime(2026, 6, 3, 14, 30, tzinfo=SH)}
    navs = {c: pd.Series([1, 2, 999, 1000], index=sessions[:4]) for c in ("A", "B")}
    source = {
        "sessions": sessions,
        "recorded_fees": {},
        "instruments": {c: {"name": c, "is_active": True, "kind": "otc"} for c in navs},
        "mapping": [
            {
                "code": c,
                "board": "示例",
                "board_id": 1,
                "mapping_source": "manual",
                "listed_on": None,
                "board_active": True,
            }
            for c in navs
        ],
        "disclosures": {
            c: {"mapped_sector": "示例", "disclosed": str(sessions[k]), "source_url": "doc"}
            for c, k in (("A", 0), ("B", 3))
        },
        "bars": {1: [{"trade_date": s, "close": k} for k, s in enumerate(sessions)]},
    }

    def check_prefix(bars, *, closed_sessions, as_of, replay):
        assert [b["trade_date"] for b in bars] == closed_sessions == sessions[:2]
        assert as_of == report["as_of"] and replay
        return {"state": "watch"}

    monkeypatch.setattr(opportunity, "assess", check_prefix)
    _, visible, _, signals, _, _ = _session_inputs(source, report, 2, navs)
    assert visible["A"].tolist() == [1, 2]
    assert [f["code"] for f in signals[0]["funds"]] == ["A"]
