from datetime import date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

spec = spec_from_file_location(
    "manager_audit", Path(__file__).resolve().parents[1] / "scripts" / "audit_manager_replay.py"
)
audit_script = module_from_spec(spec)
spec.loader.exec_module(audit_script)


def test_future_prices_cannot_change_reconstructed_issue_probability():
    from datetime import timedelta

    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(100)]
    prices = {day: 1.01**i for i, day in enumerate(days)}
    before = audit_script.recalibrate_forecast(days[:81], prices, 5)
    for d in days[81:]:
        prices[d] = 0.01
    assert before == audit_script.recalibrate_forecast(days[:81], prices, 5)
    assert before[0] == 15 and before[1] == 0.8


def test_independent_scorer_detects_false_return_and_early_confirmation():
    days = [date(2025, 6, 9), date(2025, 6, 10)]
    end = datetime(2025, 6, 10, 20, 30, tzinfo=audit_script.SH)
    row = {
        "forecast_id": 1,
        "board": "银行",
        "board_id": 1,
        "horizon_days": 1,
        "target_date": days[1],
        "base_date": days[0],
        "proxy_code": "F",
        "issued_at": end.replace(day=9, hour=14),
        "scored_at": end,
        "realized_return": 2,
        "prob_up": 0.65,
        "hit": False,
    }
    result = audit_script.Audit()
    audit_script.forecast_returns(result, [row], days, {"F": {days[0]: 1.0, days[1]: 1.01}}, end)
    assert result.failed == 3
    assert {s.split(" · ")[0] for s in result.examples} == {
        "outcome cutoff",
        "exact outcome",
        "direction",
    }


def test_v2_bias_ignores_later_scoring_and_weights_target_dates_equally():
    from datetime import timedelta

    start = date(2025, 5, 1)
    issue = datetime(2025, 8, 1, 14, 30, tzinfo=audit_script.SH)
    one = {
        "horizon_days": 1,
        "scored_at": issue - timedelta(days=2),
        "target_date": issue.date() - timedelta(days=2),
        "prob_up": 0.8,
        "realized_return": -0.03,
    }
    two = {
        **one,
        "scored_at": issue - timedelta(days=1),
        "target_date": issue.date() - timedelta(days=1),
        "prob_up": 0.5,
        "realized_return": 0.04,
    }
    items = [one] * 4 + [
        two,
        {**one, "scored_at": issue + timedelta(minutes=1)},
        {**one, "target_date": issue.date() + timedelta(days=3)},
    ]
    correct = audit_script.visible_calibration(items, 1, issue)
    assert correct["days"] == 2 and correct["latest"] == str(two["target_date"])
    assert abs(correct["shift"] - (-0.8 + 0.5) / 32) < 1e-12
    dates = [start + timedelta(days=i) for i in range(100)]
    prices = {d: 1.01**i for i, d in enumerate(dates)}
    result = audit_script.reconstruct_v2(dates[:81], prices, 5, correct)
    prices.update({d: 20 for d in dates[81:]})
    assert result == audit_script.reconstruct_v2(dates[:81], prices, 5, correct)
    assert result["samples"] == result["all_samples"] == 12


def test_calibration_verifies_full_precision_before_replaying_a_half_tick():
    rebuilt = {"days": 60, "latest": "2026-11-02", "shift": -0.07515}
    frozen = {**rebuilt, "shift": -0.07515000000000005}
    audit = audit_script.Audit()
    checked = audit_script.verified_calibration(audit, rebuilt, frozen, "5T")
    assert audit.failed == 0
    assert round(0.4 + rebuilt["shift"], 4) == 0.3249
    assert round(0.4 + checked["shift"], 4) == 0.3248

    corrupted = {**frozen, "shift": frozen["shift"] - 1e-12}
    assert audit_script.verified_calibration(audit, rebuilt, corrupted, "5T") == rebuilt
    assert audit.failed == 1


def test_post_replay_quote_has_same_series_anchor_and_does_not_modify_memory():
    base, end = date(2025, 6, 9), date(2025, 6, 16)
    forecasts = {
        5: {
            "forecast_id": 5,
            "base_date": base,
            "target_date": end,
            "board_id": 2,
            "board": "行业",
            "proxy_code": "",
            "prob_up": 0.4,
            "hit": None,
            "realized_return": None,
            "scored_at": None,
        }
    }
    prices = {2: {base: 80.0}}
    boards = {2: {"code": "303000", "name": "行业", "source": "ths"}}
    row = {
        "forecast_id": 5,
        "board_id": 2,
        "board_name": "行业",
        "board_code": "303000",
        "base_date": base,
        "target_date": end,
        "source": "ths",
        "source_url": "https://q.10jqka.com.cn/gn/detail/code/303000/",
        "base_close": 80.0,
        "target_close": 72.0,
        "original_base_close": 80.0,
        "original_target_close": None,
        "verified_at": datetime(2025, 7, 1, tzinfo=audit_script.SH),
        "realized_return": -0.1,
        "hit": True,
    }
    given = audit_script.Audit()
    supplemental = audit_script.verify_supplements(
        given, [row], forecasts, prices, boards, [base, end], 2
    )
    assert given.failed == 0 and supplemental[5]["hit"] is True
    assert forecasts[5]["realized_return"] is None and end not in prices[2]
    corrupt = {**row, "board_code": "399999", "base_close": 70.0}
    invalid = audit_script.Audit()
    audit_script.verify_supplements(invalid, [corrupt], forecasts, prices, boards, [base, end], 1)
    assert invalid.failed >= 3
