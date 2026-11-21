from copy import deepcopy
from datetime import date, datetime, time, timedelta

import pytest

from quantlab import behavior

DAY = date(2026, 11, 20)
CUTOFF = datetime.combine(DAY + timedelta(days=1), time(10), behavior.SH)


def bars():
    sessions = []
    day = DAY
    while len(sessions) < 21:
        if day.weekday() < 5:
            sessions.append(day)
        day -= timedelta(days=1)
    series = [
        {
            "trade_date": d,
            "source": "ths",
            "close": 100 + n,
            "high": 101 + n,
            "open": 100 + n,
            "low": 99 + n,
            "amount": 100.0,
            "ingested_at": datetime.combine(d, time(16), behavior.SH),
        }
        for n, d in enumerate(reversed(sessions))
    ]
    series[-1].update(open=120, high=125, low=116, close=117, amount=160)
    return series


def board(net=-1e8, five=-5e8):
    return {
        "board_id": 42,
        "board": "板块甲",
        "as_of": str(DAY),
        "proxy_code": "",
        "capital_flow": {
            "flow_status": "available",
            "flow_source": "同花顺",
            "flow_date": str(DAY),
            "net_yuan": net,
            "net_5d_yuan": five,
            "net_pct": -3.2,
        },
    }


def test_distribution_needs_runup_rejection_turnover_and_same_day_verified_flow():
    series = bars()
    s = behavior.analyze(board(), series, CUTOFF)
    assert s["regime"] == "distribution"
    assert s["risk_multiplier"] == 0.75
    assert len(s["hypotheses"]) >= 2
    assert s["hypotheses"][0]["confirm"] != s["hypotheses"][0]["invalidate"]
    assert [r["day"] for r in s["path"]] == [1, 2, 3, 4, 5]
    assert len({r["invalidate"] for r in s["path"]}) == 5
    assert next(o for o in s["observations"] if o["key"] == "amount_ratio")["value"] == 1.6
    assert next(o for o in s["observations"] if o["key"] == "net_1d")["value"] == -1
    for change in ({"amount": 100}, {"high": 120}, {"ingested_at": CUTOFF + timedelta(seconds=1)}):
        current = [*series[:-1], {**series[-1], **change}]
        result = behavior.analyze(board(), current, CUTOFF)
        assert result["regime"] != "distribution" and result["risk_multiplier"] == 1


def test_directional_selling_has_a_market_alternative_and_proxy_scope():
    series = bars()
    series[-1].update(
        high=118, open=118, close=116, low=116, benchmark_pct=-2, benchmark_source="sina"
    )
    s = behavior.analyze({**board(), "proxy_code": "000000"}, series, CUTOFF)
    assert [h["key"] for h in s["hypotheses"]] == ["supply", "market"]
    assert any("000000" in text for text in s["limitations"])
    assert any(o["key"] == "benchmark_pct" for o in s["observations"])


@pytest.mark.parametrize(
    "bad", [{"flow_status": "unverified"}, {"flow_date": "2026-11-19"}, {"flow_source": "未知"}]
)
def test_missing_or_old_data_never_becomes_zero_flow(bad):
    row = board()
    row["capital_flow"].update(bad)
    s = behavior.analyze(row, bars(), CUTOFF)
    assert s["regime"] == "insufficient" and s["risk_multiplier"] == 1
    assert "net_1d" not in {o["key"] for o in s["observations"]}


def test_before_close_rejects_today_bar_and_future_intraday_ticks():
    cutoff = datetime.combine(DAY, time(14, 30), behavior.SH)
    row = board()
    row["intraday"] = {
        "at": (cutoff + timedelta(seconds=1)).isoformat(),
        "return": -0.04,
        "capital_flow": {"source": "东方财富板块行情", "net_yuan": -100},
    }
    result = behavior.analyze(row, bars(), cutoff)
    assert result["regime"] == "insufficient" and result["observations"] == []


def test_opposite_recent_real_intraday_flow_downgrades_closed_pattern():
    cutoff = datetime.combine(date(2026, 11, 23), time(14, 30), behavior.SH)
    row = board()
    row["intraday"] = {
        "at": (cutoff - timedelta(minutes=1)).isoformat(),
        "return": 0.01,
        "capital_flow": {"source": "东方财富板块行情", "net_yuan": 20e8},
    }
    s = behavior.analyze(row, bars(), cutoff)
    assert s["regime"] == "mixed" and s["risk_multiplier"] == 1
    assert s["hypotheses"][1]["key"] == "distribution"
    assert next(o for o in s["observations"] if o["key"] == "intraday_net")["value"] == 20
    row["intraday"]["at"] = (cutoff - timedelta(minutes=31)).isoformat()
    assert behavior.analyze(row, bars(), cutoff)["regime"] == "distribution"


def test_same_index_minute_can_contradict_pressure_without_a_flow_observation():
    series = bars()
    series[-1].update(high=118, open=117.5, low=115, close=115, amount=90)
    cutoff = datetime.combine(date(2026, 11, 23), time(14, 30), behavior.SH)
    spot = {
        "source": "同花顺板块分时",
        "source_code": "881121",
        "at": cutoff.isoformat(),
        "return": 0.03,
    }
    analysis = behavior.analyze(board() | {"intraday": spot}, series, cutoff)
    assert analysis["regime"] == "mixed"
    assert analysis["hypotheses"][1]["key"] == "supply"
    assert any("越过信号日高点" in r for r in analysis["hypotheses"][1]["against"])
    assert "intraday_net" not in {o["key"] for o in analysis["observations"]}
    spot["source"] = "其他板块口径"
    assert behavior.analyze(board() | {"intraday": spot}, series, cutoff)["regime"] == "supply"


def evidence_days(n, span=1):
    result = []
    for i in range(n):
        end = DAY - timedelta(days=i * span)
        start = end - timedelta(days=span)
        result.append(
            {
                "board_id": 42,
                "proxy_code": "",
                "horizon_days": span,
                "regime": "demand",
                "issued_at": datetime.combine(start, time(14, 30), behavior.SH),
                "target_date": end,
                "base_date": start,
                "scored_at": datetime.combine(end, time(16), behavior.SH),
                "base_prob_up": 0.7,
                "realized_return": -0.03,
            }
        )
    return result


def calibration(rows, horizon=1):
    return behavior.feedback(
        rows, board_id=42, proxy_code="", horizon_days=horizon, regime="demand", as_of=CUTOFF
    )


def test_feedback_selects_sealed_estimand_only_and_caps_learned_correction():
    observed = evidence_days(11)
    extra = evidence_days(12)[-1]
    for change in (
        {"proxy_code": "000000"},
        {"board_id": 1},
        {"version": 2},
        {"regime": "supply"},
        {"realized_return": None},
        {"horizon_days": 5},
        {"scored_at": CUTOFF + timedelta(minutes=1)},
        {"scored_at": extra["issued_at"]},
        {"issued_at": CUTOFF + timedelta(days=1)},
    ):
        assert calibration([*observed, {**extra, **change}])["samples"] == 11
    small = calibration(observed)
    assert small["up_rate"] == 0 and small["probability_shift"] == 0
    large = calibration([*observed, extra])
    assert large["probability_shift"] == -0.05
    assert large["brier_base"] == pytest.approx(0.49)
    assert large["mean_return"] == pytest.approx(-0.03)


def test_multi_session_learning_excludes_overlapping_returns_and_duplicates():
    x = evidence_days(12, span=5)
    overlapping = deepcopy(x[0])
    for key in ("base_date", "target_date", "scored_at", "issued_at"):
        overlapping[key] -= timedelta(days=1)
    assert calibration([*x, *x, overlapping], 5) == calibration(x, 5)


def test_comparison_scores_frozen_first_issue_probabilities_on_identical_targets():
    observations = evidence_days(13)
    observations[0].update(prob_up=0.6)
    observations[1].update(realized_return=0.02, prob_up=0.8)
    observations[2].update(prob_up=1.3)
    result = calibration(observations)
    assert result["samples"] == 13 and result["paired_samples"] == 2
    assert result["brier_base_paired"] == pytest.approx((0.7**2 + 0.3**2) / 2)
    assert result["brier_adjusted"] == pytest.approx((0.6**2 + 0.2**2) / 2)
    assert result["brier_gain"] == pytest.approx(0.09)
    assert calibration([])["brier_gain"] is None
