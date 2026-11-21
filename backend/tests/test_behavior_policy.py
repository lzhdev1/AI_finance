from quantlab import behavior_policy as policy


def test_fund_nav_and_cost_can_veto_new_exposure_but_preserve_reductions():
    forecasts = [
        {"code": "A", "horizon_days": 1, "prob_up": 0.82, "expected_return": 0.023},
        {"code": "A", "horizon_days": 5, "prob_up": 0.5263, "expected_return": 0.000472},
    ]
    costs = {
        "A": {
            "round_trip_fee": 1 / 0.985 - 1,
            "redemption_rate": 0.015,
            "holding_days": 4,
            "application_date": "2026-11-23",
            "evaluation_date": "2026-11-27",
        }
    }
    initial = {"A": 0.35, "no_nav": 0.1, "reduce": 0.04}
    target, notes = policy.fund_entry_limit(initial, {"A": 0.08, "reduce": 0.12}, forecasts, costs)
    assert target == {"A": 0.08, "reduce": 0.04}
    assert initial["A"] == 0.35 and len(notes) == 2
    assert all(s in notes[0] for s in ("1.52%", "1.50%", "4 个自然日", "2026-11-23", "2026-11-27"))
    assert policy.fund_entry_limit(None, {}, [], {}) == (None, [])


def test_behavior_risk_applies_once_to_new_shares_in_a_shared_fund():
    def watch(factor, intent="allocate"):
        return {
            "intent": intent,
            "funds": [{"code": "same"}],
            "behavior": {"risk_multiplier": factor},
        }

    rows = [watch(0.75), watch(0.75), watch(1, "observe")]
    held = {"same": 0.20}
    assert policy.limit_additions(rows, {"same": 0.32}, held) == {"same": 0.29}
    assert rows[0]["behavior"]["impact"]["before_weight"] == 0.32
    assert rows[0]["behavior"]["impact"]["after_weight"] == 0.29
    assert rows[2]["behavior"]["impact"]["before_weight"] == 0
    assert policy.limit_additions(rows, {"same": 0.1}, held) == {"same": 0.1}


def test_behavior_claim_needs_support_for_that_particular_board_and_uncertainty():
    row = {"board_id": 15, "board": "银行", "behavior": {"regime": "distribution"}}
    assert policy.hypotheses_cited("银行可能存在拉高兑现风险。", [row], ["H15"])
    assert not policy.hypotheses_cited("银行正在派发。", [row], ["H15"])
    assert not policy.hypotheses_cited("银行可能存在拉高兑现风险。", [row], ["H151"])
    row["behavior"]["regime"] = "insufficient"
    assert not policy.hypotheses_cited("银行疑似出货。", [row], ["H15"])
