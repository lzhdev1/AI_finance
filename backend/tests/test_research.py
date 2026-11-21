from datetime import date, datetime

import pandas as pd

from quantlab import learning, research


def test_forecast_uses_non_overlapping_mature_outcomes():
    prices = pd.Series([1.01**i for i in range(101)])
    estimate = research._forecast(prices, 5)
    # At most one completed five-session example per five sessions.
    assert estimate["samples"] == 19
    assert estimate["prob_up"] == round(24 / 29, 4)
    assert estimate["expected_return"] == round((1.01**5 - 1) * 19 / 29, 6)
    assert research._forecast(prices.head(30), 5) is None


def test_intraday_and_weekend_forecast_target_dates(monkeypatch):
    days = list(pd.bdate_range("2026-11-16", periods=12).date)
    monkeypatch.setattr(research.db, "query", lambda *args: [{"trade_date": d} for d in days])
    cutoff = datetime(2026, 11, 20, 14, 30, tzinfo=research.SH)
    closed, base, due = research._sessions(cutoff)
    assert closed[-1] == date(2026, 11, 19)
    assert base == date(2026, 11, 20)
    assert due == {1: date(2026, 11, 23), 5: date(2026, 11, 27)}
    assert research._sessions(cutoff.replace(day=21))[1:] == (base, due)


def test_model_must_choose_candidates_and_cite_their_evidence():
    pool = [{"board_id": 33, "evidence_ids": ["B33", "N120"]}]
    response = {
        "stance": "normal",
        "risk": "估值回落",
        "thesis": "观察动量",
        "thesis_refs": ["B33"],
        "watch": [{"board_id": 33, "intent": "observe", "reason": "观察调整", "refs": ["N120"]}],
    }
    assert research._selection(pool, response)[0][0]["board_id"] == 33
    unsupported = {**response, "_presented": {"boards": {33: ["B33", "M33-5"]}, "macro_refs": []},
                   "thesis_refs": ["M33-5"]}
    assert research._selection(pool, unsupported) is None
    for field, value in (("board_id", 17), ("refs", ["N66"]), ("refs", [{"x": 1}])):
        bad = {**response, "watch": [{**response["watch"][0], field: value}]}
        assert research._selection(pool, bad) is None


def test_allocations_honor_memory_and_two_horizon_evidence():
    watch = [
        {
            "funds": [{"code": "A"}],
            "intent": "allocate",
            "memory_factor": 0.5,
            "forecast_factor": 0.5,
            "momentum_5": 0.12,
            "momentum_20": 0.10,
        }
    ]
    target = research._targets(watch, "normal")["A"]
    assert target == round(min(0.75, research.load_settings().agent.max_board_exposure) * 0.25, 6)
    assert research._targets(watch, "cash") == {}
    assert research._targets([], "normal") is None
    down = {"prob_up": 0.42, "expected_return": -0.01}
    skewed = {"prob_up": 0.42, "expected_return": 0.01}
    assert research._forecast_budget([down, down])[0] == 0
    assert research._forecast_budget([skewed, down])[0] == 0.5
    assert research._forecast_budget([skewed, skewed])[0] == 0.75


def test_unreferenced_claims_do_not_become_model_decisions():
    row = {"board_id": 4, "evidence_ids": ["B4"], "momentum_5": 0.015239}
    response = {
        "stance": "reduced",
        "thesis": "保留现金",
        "risk": "短期反转",
        "thesis_refs": ["B4"],
        "watch": [
            {"board_id": 4, "intent": "allocate", "reason": "近五日上涨1.52%", "refs": ["B4"]}
        ],
    }
    assert research._selection([row], response)
    response["watch"][0]["reason"] = "近五日上涨99.9%"
    assert research._selection([row], response) is None
    response["watch"][0]["reason"] = "近五日上涨1.52%"
    response["thesis_refs"] = ["N999999"]
    assert research._selection([row], response) is None


def test_observation_is_not_allocation_and_missing_intent_is_invalid():
    market = {
        "board_id": 1,
        "board": "保险",
        "funds": [{"code": "A", "name": "保险基金"}],
        "momentum_5": 0.1,
        "momentum_20": 0.1,
        "memory_factor": 1.0,
        "forecast_factor": 0.5,
        "evidence_ids": ["B1"],
        "reason": "短期动量为正。",
    }
    response = {
        "stance": "reduced",
        "thesis": "保险仅观察，短期动量为正。",
        "risk": "走势反转",
        "thesis_refs": ["B1"],
        "watch": [{"board_id": 1, "intent": "observe", "refs": ["B1"], "reason": "保险仅观察。"}],
    }
    rows, stance, view, risk = research._selection([market], response)
    assert view == "短期动量为正。" and rows[0]["reason"] == "短期动量为正。"
    assert research._targets(rows, stance) == {}
    assert "现金 100.00%" in research._allocation_summary({}, rows)
    selected = {
        **market,
        "board_id": 2,
        "board": "科技",
        "intent": "allocate",
        "funds": [{"code": "B"}],
    }
    assert set(research._targets([*rows, selected], stance)) == {"B"}
    assert research._targets([*rows, {**selected, "funds": [{"code": "A"}]}], stance) == {}
    assert research._targets(rows, stance, has_investable_data=False) is None
    del response["watch"][0]["intent"]
    assert research._selection([market], response) is None


def test_calibration_measures_up_days_and_evaluates_unseen_dates(monkeypatch):
    # A forecaster who always predicts a drop has all hits when prices fall.
    # The observed probability of an up day in that training period is zero.
    frames = pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-01", periods=60),
            "up": [0] * 48 + [1] * 12,
            "prob_up": [0.3] * 60,
        }
    )
    monkeypatch.setattr(learning.db, "query_df", lambda *args: frames)
    result = learning.calibration()
    assert result["enabled"] and result["validation_samples"] == 12
    assert all(r["actual"] == 0 for r in result["bins"])
    assert result["brier_calibrated"] == 1.0
    assert result["brier_raw"] < result["brier_calibrated"]
