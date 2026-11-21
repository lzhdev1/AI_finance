import json

import pytest

from quantlab import replay_archive


def test_failed_flush_keeps_last_complete_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "v2.json"
    replay_archive.write_snapshot(path, {"days": 69})
    def fail(_fd):
        raise OSError("simulated disk flush failure")
    monkeypatch.setattr(replay_archive.os, "fsync", fail)
    with pytest.raises(OSError):
        replay_archive.write_snapshot(path, {"days": 70})
    assert json.loads(path.read_bytes()) == {"days": 69}
    assert list(tmp_path.iterdir()) == [path]


def test_maximum_drawdown_includes_opening_cash_and_earlier_peaks():
    assert replay_archive.max_drawdown([0.8, 1.1]) == pytest.approx(-0.2)
    assert replay_archive.max_drawdown([1.2, 0.9, 1.15]) == pytest.approx(-0.25)
    assert replay_archive.max_drawdown([]) == 0


def test_directional_denominator_brier_and_unverified_dates():
    def prediction(days, p, outcome, hit, end="2026-11-20"):
        return {
            "horizon_days": days,
            "up_probability": p,
            "actual_return": outcome,
            "hit": hit,
            "target_date": end,
        }

    one, five = replay_archive.forecast_stats(
        [
            prediction(1, 0.70, 0.01, True),
            prediction(1, 0.30, 0.01, False),
            prediction(1, 0.50, -0.05, None),
            prediction(1, 0.60, None, None),
            prediction(1, 0.61, None, None, "2026-11-23"),
            prediction(5, 0.60, 0, False),
        ],
        "2026-11-20",
    )
    assert (one["total"], one["scored"], one["hits"], one["directional"], one["neutral"]) == (
        5,
        3,
        1,
        2,
        1,
    )
    assert one["hit_rate"] == 0.5 and one["baseline_up_rate"] == 1
    assert one["brier"] == pytest.approx((0.09 + 0.49 + 0.25) / 3)
    assert one["overdue"] == 1 and one["not_due"] == 1
    assert five["brier"] == pytest.approx(0.36) and five["hit_rate"] == 0


def test_archive_action_pages_read_consistent_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "replay.json"
    monkeypatch.setattr(replay_archive, "ARCHIVE", path)
    assert replay_archive.current()["summary"]["status"] == "empty"
    contents = {
        "summary": {"status": "running"},
        "days": [
            {"trade_date": "third", "action": "open"},
            {"trade_date": "second", "action": "hold"},
            {"trade_date": "first", "action": "open"},
        ],
    }
    path.write_text(json.dumps(contents), encoding="utf-8")
    assert replay_archive.days_page("open", 0, 1)["has_more"]
    assert replay_archive.days_page("open", 1, 1)["items"] == [contents["days"][2]]
    assert replay_archive.days_page("close", 0, 20)["total"] == 0
    replacement = tmp_path / "new.json"
    replacement.write_text(
        json.dumps({"summary": {"status": "ready"}, "days": []}), encoding="utf-8"
    )
    replacement.replace(path)
    assert replay_archive.current()["summary"]["status"] == "ready"
