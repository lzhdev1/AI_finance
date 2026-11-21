from datetime import date, timedelta

from quantlab.opportunity_research import realized_memories


def test_memory_uses_only_complete_non_overlapping_observations():
    sessions = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    points = {(1, d): {"low": 90, "high": 120, "close": 100} for d in sessions}
    # The entry day's low was unknown when applying but cannot be captured after the close.
    for i in range(0, 48, 6):
        points[1, sessions[i]]["low"] = 50
        points[1, sessions[i + 5]]["close"] = 110
    observations = [(1, "retest", d) for d in sessions[:45]]
    data = realized_memories(observations * 2 + [(2, "retest", sessions[0])], points, sessions[:48])
    assert data[1, "retest"]["samples"] == 8
    assert data[1, "retest"]["up_rate"] == 1
    assert round(data[1, "retest"]["median_adverse_move"], 2) == -0.10
    assert (2, "retest") not in data
    del points[1, sessions[46]]
    data = realized_memories(observations, points, sessions[:48])
    assert data[1, "retest"]["samples"] == 7
    assert data[1, "retest"]["median_adverse_move"] is None
