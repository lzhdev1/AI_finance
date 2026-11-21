from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from quantlab import replay_attribution as attr

DAY = date(2026, 11, 2)


def fill(ident, day, side, amount, shares, nav, fee):
    return {
        "order_id": ident,
        "date": day,
        "side": side,
        "code": "X",
        "name": "test",
        "board": "sector",
        "board_id": 1,
        "mapping_source": "report",
        "amount": D(amount),
        "shares": D(shares),
        "nav": D(nav),
        "fee": D(fee),
    }


def test_fifo_respects_recorded_gross_cashflow_and_mixed_redemption_rates():
    day2, exit_day, end = [DAY + timedelta(days=n) for n in (3, 7, 9)]
    orders = [
        fill(1, DAY, "buy", "102", "100", "1", "2"),
        fill(2, day2, "buy", "200", "100", "2", "0"),
        fill(3, exit_day, "sell", "187.50", "150", "1.25", ".9375"),
    ]
    lots = attr.fifo_lots(
        orders, {"X": {"sell": [[7, 0.015], [999999, 0]]}}, end, {"X": {end: D("1.4")}}
    )
    assert [r["sell_fee"] for r in lots] == [D(0), D(".9375"), D(0)]
    assert [r["buy_order_id"] for r in lots] == [1, 2, 2]
    assert [r["status"] for r in lots] == ["closed", "closed", "open"]
    assert [r["net_pnl"] for r in lots] == [D(23), D("-38.4375"), D(-30)]
    assert sum(r["net_pnl"] for r in lots) == D("187.50") - D(".9375") + D(70) - D(302)


def test_fifo_fails_closed_on_unexplained_oversell_or_mixed_fee_mismatch():
    end = DAY + timedelta(days=7)
    with pytest.raises(ValueError, match="赎回份额"):
        attr.fifo_lots([fill(1, end, "sell", "2", "1", "2", "0")], {}, end, {})
    fills = [
        fill(1, DAY, "buy", "2", "1", "2", "0"),
        fill(2, DAY, "buy", "2", "1", "2", "0"),
        fill(3, end, "sell", "4", "2", "2", "1"),
    ]
    with pytest.raises(ValueError, match="档位"):
        attr.fifo_lots(fills, {"X": {"sell": [[7, 0.015], [9999, 0]]}}, end, {})


def test_market_selection_tracking_and_lookaround_never_use_nearest_prices():
    days = [DAY + timedelta(days=i) for i in range(15)]
    row = {
        "entry_date": days[5],
        "mark_date": days[7],
        "status": "closed",
        "shares": D(10),
        "entry_nav": D(2),
        "exit_nav": D("1.90"),
        "gross_pnl": D("-1.01"),
    }
    fund = {days[0]: D("1.6"), days[5]: D(2), days[7]: D("1.90"), days[13]: D("2.2")}
    market = {days[5]: D(100), days[7]: D(110)}
    board = {days[5]: D(100), days[7]: D(106)}
    result = attr._timing_and_effects(row, days, market, board, fund)
    assert [result[k] for k in ("market_pnl", "selection_pnl", "tracking_pnl", "rounding_pnl")] == [
        D(2),
        D("-.8"),
        D("-2.2"),
        D("-.01"),
    ]
    assert result["exit_next_5d_return"] is None and result["entry_prior_5d_return"] == D(".25")
    missing = attr._timing_and_effects(row, days, market, {}, fund)
    assert missing["selection_pnl"] is None and missing["tracking_pnl"] is None
    assert missing["unattributed_pnl"] == D(-3)


def test_fixed_duration_reference_uses_only_common_exact_endpoints_and_reconciles():
    dates = [DAY + timedelta(days=n) for n in range(4)]
    benchmark = dict(zip(dates, map(D, ["100", "110", "99", "101"])))
    board = dict(zip(dates[:3], map(D, ["100", "120", "108"])))
    ref = attr._reference_windows(dates, 1, benchmark, board)
    assert ref == (2, D(0), D(".05"))
    effects = attr._window_effects(
        {"entry_nav": D(2), "shares": D(100), "market_pnl": D(20), "selection_pnl": D(20)}, ref
    )
    assert effects["typical_selection_pnl"] == effects["selection_timing_pnl"] == D(10)
    assert effects["market_timing_pnl"] == D(20)
    assert attr._reference_windows(dates, 4, benchmark, board) == (0, None, None)


def test_neutral_contributes_to_brier_pending_is_excluded_and_inputs_remain_frozen():
    def forecast(prob, aver, hits, momentum, post=False):
        return {
            "horizon_days": 5,
            "prob_up": prob,
            "samples": 9,
            "actual": aver,
            "hit": hits,
            "momentum": momentum,
            "score": 87,
            "board": "原板块",
            "supplement": post,
            "target_date": DAY + timedelta(days=1 if aver is None else 0),
        }

    raw = [
        forecast(0.6, -0.01, False, 0.03),
        forecast(0.5, 0.05, None, -0.01, True),
        forecast(0.45, None, None, -0.1),
    ]
    result = attr.diagnose(raw, DAY, ["冻结方法"])[1]
    assert result["total"] == 3 and result["scored"] == 2 and result["directional"] == 1
    assert result["reversal_sample"] == result["reversal_count"] == 2
    assert result["brier"] == pytest.approx((0.36 + 0.25) / 2)
    assert result["pending"] == 1 and result["overdue"] == 0 and result["post_verified"] == 1
    assert sum(b["total"] for b in result["probability_bins"]) == 3


def test_held_fund_topup_uses_only_frozen_prior_actual_purchase_mapping():
    days = [DAY + timedelta(days=i) for i in range(4)]
    orders = [fill(i + 1, d, "buy", "2", "1", "2", "0") for i, d in enumerate(days)]
    sector = {
        "board_id": 2,
        "board": "A",
        "intent": "allocate",
        "funds": [{"code": "X", "name": "当时名称", "mapping_source": "name-match"}],
    }
    other = {**sector, "board_id": 4, "board": "B"}
    reports = {
        days[0]: {},
        days[1]: {"watchlist": [sector]},
        days[2]: {},
        days[3]: {"watchlist": [other]},
    }
    attr._map_orders(orders, reports)
    assert [r["board"] for r in orders] == [None, "A", "A", "B"]
    assert orders[2]["mapping_basis"] == "先前实际申购的冻结映射"
    assert orders[2]["mapping_date"] == days[1]


def test_disclosed_redemption_reports_only_covered_personal_c_class_fees(monkeypatch):
    monkeypatch.setattr(attr.db, "query", lambda *args: [])
    day = date(2026, 10, 13)
    orders = [
        {"side": "sell", "date": day, "code": "008089", "order_id": 1, "fee": D(0)},
        {"side": "sell", "date": day, "code": "unknown", "order_id": 2, "fee": D(7)},
    ]
    lots = [
        {
            "status": "closed",
            "code": o["code"],
            "exit_date": day,
            "holding_days": 7,
            "sell_order_id": o["order_id"],
            "shares": D(2000),
            "exit_nav": D(1),
        }
        for o in orders
    ]
    notes, audit, sources = attr._review_redemption_fees(orders, lots)
    assert audit["covered_lots"] == audit["uncovered_lots"] == 1
    assert audit["covered_difference"] == D(10)
    assert audit["uncovered_booked_fees"] == D(7)
    assert audit["uncovered_codes"] == ["unknown"]
    assert audit["changed_orders"][0]["order_id"] == 1
    assert sources[0]["published"] == "2026-05-29"
    assert sources[0]["page"] == 3
    assert "0.50%" in notes[-1]
