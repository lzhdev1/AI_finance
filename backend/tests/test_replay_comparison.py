import json

import pytest

from quantlab import replay_archive, replay_comparison


def test_cash_day_reasons_read_the_frozen_execution_checks_once_per_day():
    def day(weight, check="", rationale="", cash=1000):
        return {
            "cash": cash,
            "total_value": 1000,
            "risk_checks": [check],
            "rationale": rationale,
            "targets": [{"target_weight": weight}] if weight else [],
        }

    rows = [
        day(0.2, "申购最多动用 0.00 元，受可用现金和当月换手额度约束"),
        day(0.2, "申购最多动用 125.24 元，受可用现金和当月换手额度约束"),
        day(0, rationale="已确认净值回撤 -12.50% 触及 12% 硬止损线，覆盖研究目标，转为现金"),
        day(0),
        day(0.2),
        day(0.2, cash=800),
    ]
    counts = replay_comparison.cash_day_reasons(rows)
    assert counts == dict.fromkeys(
        ("budget_zero", "budget_limited", "hard_stop", "no_target", "other"), 1
    )


def test_versioned_read_does_not_mix_archives(tmp_path, monkeypatch):
    legacy = tmp_path / "latest.json"
    monkeypatch.setattr(replay_archive, "ARCHIVE", legacy)

    def write(path, name):
        path.write_text(
            json.dumps({"summary": {"id": name}, "days": [{"action": "hold", "name": name}]})
        )

    write(legacy, "legacy")
    assert replay_archive.summary()["id"] == "legacy"
    write(tmp_path / "v1.json", "baseline")
    write(tmp_path / "v2.json", "candidate")
    assert replay_archive.days_page("hold", 0, 10)["items"][0]["name"] == "baseline"
    assert replay_archive.days_page("hold", 0, 10, "v2")["items"][0]["name"] == "candidate"
    assert replay_archive.current()["summary"]["id"] == "legacy"
    with pytest.raises(ValueError):
        replay_archive.version_path("../latest")


def test_opportunity_evaluation_keeps_june_audit_when_experiment_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_archive, "ARCHIVE", tmp_path / "latest.json")
    monkeypatch.setattr(replay_comparison, "CONFIG_DIR", tmp_path)
    (tmp_path / "replay_june_2026_audit.json").write_text('{"period": "2026-06"}')
    assert replay_comparison.opportunity_evaluation() == {
        "status": "empty", "available": False, "june_audit": {"period": "2026-06"},
    }
    (tmp_path / "opportunity-evaluation.json").write_text('{"status": "completed", "days": []}')
    result = replay_comparison.opportunity_evaluation()
    assert result["available"] and result["status"] == "completed"
    assert result["days"] == [] and result["june_audit"] == {"period": "2026-06"}


def test_paired_predictions_require_same_instrument_endpoint_and_mature_outcome():
    def record(sector, p, hit, ret=0.02, proxy=""):
        return {
            "sector_name": sector,
            "up_probability": p,
            "actual_return": ret,
            "hit": hit,
            "horizon_days": 1,
            "target_date": "2026-10-13",
            "proxy_code": proxy,
        }

    def day(rows):
        return {"days": [{"trade_date": "2026-10-12", "forecasts": rows}]}

    a = day([record("甲", 0.65, True), record("乙", 0.5, None), record("丙", 0.55, True)])
    b = day(
        [record("甲", 0.4, False), record("乙", 0.6, True), record("丙", 0.58, True, proxy="123")]
    )
    one, five = replay_comparison.matched_forecasts(a, b)
    assert one["scored"] == 2 and one["directional"] == 1
    assert one["original_hits"] == 1 and one["optimized_hits"] == 0
    assert one["original_brier"] == pytest.approx((0.35**2 + 0.5**2) / 2)
    assert five["original_brier"] is None
    b["days"][0]["forecasts"][0]["actual_return"] = -0.01
    with pytest.raises(ValueError):
        replay_comparison.matched_forecasts(a, b)
