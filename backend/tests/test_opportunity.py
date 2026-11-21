import json
from datetime import datetime, time

import pandas as pd
import pytest

from quantlab import opportunity as op
from quantlab.behavior import SH
from quantlab.pods import PodAccount


DAYS = list(pd.bdate_range("2026-01-01", periods=60).date)


def dataset():
    data = []
    for idx, day in enumerate(DAYS[:21]):
        price = 100 + 0.3 * idx
        data.append(
            dict(
                trade_date=day,
                source="ths",
                high=price + 0.6,
                low=price - 0.5,
                close=price,
                amount=100,
                benchmark_pct=0.1,
                ingested_at=datetime.combine(day, time(17), SH),
            )
        )
    data[-1].update(high=108.2, low=105.6, close=108.1, amount=145)
    return data


def scan(data, **kwargs):
    return op.assess(
        data,
        closed_sessions=DAYS[:22],
        as_of=datetime.combine(DAYS[21], time(14, 50), SH),
        **kwargs,
    )


def test_breakout_distinguishes_observables_and_missing_flows():
    result = scan(dataset())
    assert result["state"] == "breakout" and result["eligible"]
    assert result["signal_date"] == str(DAYS[20])
    assert 103 < result["support"] < 106.3
    assert result["metrics"]["amount_ratio"] == pytest.approx(1.45)
    assert result["metrics"]["analog_count"] == 0
    assert result["metrics"]["flow_net_yuan"] is None
    assert result["probe_weight"] == 0.03


def test_missing_sessions_and_post_cutoff_rows_cannot_create_a_signal():
    bars = dataset()
    same_day_close = dict(bars[-1], trade_date=DAYS[21], close=200)
    assert scan(bars + [same_day_close]) == scan(bars)
    assert scan(bars[:10] + bars[11:])["state"] == "insufficient"
    assert scan([*bars[:-1], dict(bars[-1], benchmark_pct=None)])["state"] == "insufficient"
    assert scan([dict(bars[0], source="eastmoney"), *bars[1:]])["state"] == "insufficient"
    late = dict(bars[-1], ingested_at=datetime.combine(DAYS[21], time(15), SH))
    assert scan([*bars[:-1], late])["state"] == "insufficient"
    assert scan([*bars[:-1], late], replay=True)["state"] == "breakout"


def test_distinct_risk_and_retest_states():
    bars = dataset()
    for i, b in enumerate(bars):
        b.update(close=100 + i, high=100.8 + i, low=99.3 + i)
    bars[-1].update(high=122.0, low=116.0, close=116.3, amount=150)
    assert scan(bars)["state"] == "distribution"
    bars[-1].update(high=117, low=111, close=112)
    assert scan(bars)["state"] == "breakdown"
    bars[-1].update(high=120.4, low=115, close=120, amount=110)
    assert scan(bars)["state"] == "retest"


def test_timestamp_and_verification_required_to_veto_an_entry():
    bars = dataset()
    flow = dict(
        flow_status="available",
        flow_source="同花顺",
        flow_date=str(DAYS[20]),
        net_yuan=-1e9,
        net_pct=-3,
    )
    assert not scan(bars, flow=flow)["eligible"]
    assert scan(bars, flow=dict(flow, flow_status="unverified"))["eligible"]
    assert scan(bars, flow=dict(flow, flow_date=str(DAYS[19])))["eligible"]
    spot = {
        "source": "东方财富板块行情",
        "at": f"{DAYS[21]}T14:47:00+08:00",
        "captured_at": f"{DAYS[21]}T14:48:00+08:00",
        "return": -0.036,
    }
    assert not scan(bars, intraday=spot)["eligible"]
    ordinary = dataset()
    ordinary[-1]["amount"] = 100
    observed = scan(ordinary, intraday=dict(spot, **{"return": 0.04}))
    assert observed["state"] == "watch" and observed["metrics"]["intraday_alert"]
    assert not observed["eligible"]
    assert scan(bars, intraday=dict(spot, captured_at=f"{DAYS[21]}T14:51:00+08:00"))["eligible"]
    em = dict(spot, **{"return": 0.002}, capital_flow={"net_yuan": -1e8, "net_pct_points": -2.5})
    veto = scan(bars, intraday=em)
    assert not veto["eligible"] and veto["metrics"]["intraday_flow_net_pct_points"] == -2.5
    assert scan(bars, intraday=dict(em, source="同花顺板块分时"))["eligible"]


def test_same_day_unit_nav_needs_live_postclose_receipt(monkeypatch):
    observed = []

    def query(sql, params):
        observed.append((sql, params))
        return []

    monkeypatch.setattr(op.db, "query", query)
    for when, replay, visible in (
        (time(14, 55), False, False),
        (time(15), False, False),
        (time(20), False, True),
        (time(20), True, False),
    ):
        cutoff = datetime.combine(DAYS[21], when, SH)
        op.load_navs(["A"], DAYS[:22], cutoff, replay=replay)
        sql, params = observed[-1]
        assert (DAYS[21] in params[1]) == visible
        assert "n.ingested_at<=%s" in sql and params[3] == cutoff
        assert "n.ingested_at >=" in sql


def test_analogs_enter_next_close_and_wait_for_complete_independent_outcome():
    bars = dataset()
    bars.extend(
        dict(
            bars[-1],
            trade_date=d,
            ingested_at=datetime.combine(d, time(18), SH),
            close=p,
            high=p + 0.2,
            low=p - 0.2,
            amount=100,
        )
        for d, p in zip(DAYS[21:27], [118, 116, 115, 113, 112, 111])
    )
    cutoff = datetime.combine(DAYS[27], time(14, 50), SH)
    inputs = [op._bar(row, cutoff, False) for row in bars]
    assert op._analogs(inputs[:-1], "breakout")["analog_count"] == 0
    stats = op._analogs(inputs, "breakout")
    assert stats["analog_count"] == 1
    assert stats["analog_mean_5d"] == pytest.approx(111 / 118 - 1)
    assert stats["analog_median_adverse"] == pytest.approx(110.8 / 118 - 1)


def test_disclosure_and_fee_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(op.config, "CONFIG_DIR", tmp_path)
    row = dict(
        source="source",
        published_on="2026-06-01",
        effective_from="2026-06-03",
        effective_until="2026-06-12",
        buy=0,
        sell=[[7, 0.015], [None, 0]],
    )
    later = dict(row, published_on="2026-06-10", buy=0.001)
    (tmp_path / "opportunity_fees.json").write_text(json.dumps({"funds": {"A": [row, later]}}))

    def read(day):
        return op.disclosed_fees({}, ["A"], day).get("A")

    assert read("2026-06-02") is None
    assert read("2026-06-10")["buy"] == 0
    assert read("2026-06-12")["buy"] == 0.001
    assert read("2026-06-13") is None
    assert op.fee_cost(read("2026-06-03"), "2026-06-03", "2026-06-09") == pytest.approx(
        1 / 0.985 - 1
    )
    assert op.fee_cost(read("2026-06-03"), "2026-06-03", "2026-06-10") == 0
    assert op.fee_cost({"sell": [(999999, 0)]}, DAYS[0], DAYS[5]) is None
    assert op.fee_cost({"buy": 0, "sell": [(3, 0.01)]}, DAYS[0], DAYS[5]) is None


def candidate():
    return dict(scan(dataset()), board="甲", board_id=1, funds=[dict(code="A", name="甲基金")])


def navs():
    return {"A": pd.Series([1 + i * 0.003 for i in range(21)], index=DAYS[:21])}


def test_execution_requires_separately_observed_nav_fees_cash_and_no_existing_board_exposure():
    s = candidate()

    def decide(book=None, histories=None, fee=0, cash=1):
        return op.apply_probe(
            book or {},
            {},
            [s],
            navs() if histories is None else histories,
            {"A": fee},
            closed_sessions=DAYS[:21],
            day=DAYS[21],
            cash_weight=cash,
        )

    assert decide()[1]["action"] == "enter"
    assert decide(histories={})[1]["action"] == "idle"
    assert decide(histories={"A": navs()["A"].drop(DAYS[12])})[1]["action"] == "idle"
    assert decide(fee=None)[1]["action"] == "idle"
    assert decide(fee=0.02)[1]["action"] == "idle"
    assert decide(cash=0.015)[1]["action"] == "idle"
    s["funds"].append({"code": "B", "name": "同板块基金"})
    assert decide(book={"B": 0.2})[1]["action"] == "idle"
    assert decide(book={"C": 0.975})[0]["A"] == pytest.approx(0.025)


def test_trial_lifecycle_ignores_unfilled_buys_and_waits_for_actual_exit():
    s = candidate()

    def decide(i, held, prev=None, signal=s):
        signal = dict(signal, signal_date=str(DAYS[i - 1]))
        histories = {"A": pd.Series([1 + j * 0.003 for j in range(i)], index=DAYS[:i])}
        return op.apply_probe(
            dict(held),
            held,
            [signal],
            histories,
            {"A": 0},
            closed_sessions=DAYS[:i],
            day=DAYS[i],
            previous=prev,
            cash_weight=0.9,
        )

    targets, enter = decide(21, {})
    assert (
        op.apply_probe(
            {}, {}, [s], navs(), {"A": 0}, closed_sessions=DAYS[:23], day=DAYS[23], cash_weight=1
        )[1]["action"]
        == "idle"
    )
    held = {"A": targets["A"] * 1.2}
    assert decide(22, {}, enter)[1]["action"] == "enter"  # frozen but unfilled
    targets, holding = decide(22, held, enter)
    assert holding["action"] == "hold" and targets == held
    bad = dict(s, metrics={"close": s["support"] - 0.01}, state="watch")
    targets, exit_plan = decide(23, held, holding, bad)
    assert not targets and exit_plan["action"] == "exit"
    assert decide(24, held, exit_plan)[1]["action"] == "exit"
    prev = exit_plan
    for i in range(24, 29):
        _, prev = decide(i, {}, prev)
        assert prev["action"] == "idle"
    assert decide(29, {}, prev)[1]["action"] == "enter"
    assert decide(25, held, holding)[1]["action"] == "hold"
    assert decide(26, held, holding)[1]["action"] == "exit"


def test_monthly_reserve_never_expands_turnover_and_uses_actual_cash():
    account = PodAccount(100000, 100000, min_trade_amount=200)
    prices = {"A": 1.0, "B": 1.0}
    plan = {"action": "enter", "code": "A"}
    orders, _ = op.prepare_orders(account, {"A": 0.03, "B": 0.7}, prices, 7000, plan)
    assert [(r["code"], r["amount"]) for r in orders] == [("B", 2000), ("A", 3000)]
    assert orders[-1]["opportunity_probe"]
    orders, _ = op.prepare_orders(
        account,
        {"A": 0.03, "B": 0.7},
        prices,
        7000,
        plan,
        month_probe_buys=3000,
        cash_receivable=95400,
    )
    assert [(r["code"], r["amount"]) for r in orders] == [("B", 2600), ("A", 2000)]
    orders, _ = op.prepare_orders(
        account, {"A": 0.03, "B": 0.7}, prices, 7000, plan, month_probe_buys=5000
    )
    assert [(r["code"], r["amount"]) for r in orders] == [("B", 7000)]
    account.buy(DAYS[0], "B", 6000, 1, "core")
    orders, _ = op.prepare_orders(account, {"A": 0.03}, prices, 7000, plan, redeemable={"B": 6000})
    assert [(r["code"], r["side"], r["amount"]) for r in orders] == [
        ("B", "sell", 6000),
        ("A", "buy", 1000),
    ]
    orders, _ = op.prepare_orders(account, {}, prices, 100, {}, redeemable={"B": 0})
    assert orders == []


def test_isolated_monthly_probe_budget_survives_risk_sales_but_cannot_be_recycled():
    account = PodAccount(100000, 100000, min_trade_amount=200)
    prices = {"A": 1.0, "B": 1.0, "C": 1.0}
    plan = {"action": "enter", "code": "A"}
    account.buy(DAYS[0], "B", 10000, 1, "core")

    def orders(targets, gross, bought, **kwargs):
        return op.prepare_orders(
            account,
            targets,
            prices,
            max(0, 60000 - gross),
            plan,
            isolated_reserve=True,
            total_month_turnover=gross,
            month_probe_buys=bought,
            **kwargs,
        )

    pending, notes = orders({"A": 0.03, "C": 0.2}, 65000, 0)
    assert [(o["code"], o["side"], o["amount"]) for o in pending] == [
        ("B", "sell", 10000),
        ("A", "buy", 3000),
    ]
    assert any("超过" in text for text in notes)
    pending, _ = orders({"A": 0.03}, 77000, 4000)
    assert [o["amount"] for o in pending if o["side"] == "buy"] == [1000]
    pending, _ = orders({"A": 0.03}, 77000, 5000)
    assert all(o["side"] == "sell" for o in pending)
    pending, _ = orders({"A": 0.03}, 65000, 0, cash_receivable=account.cash)
    assert all(o["side"] == "sell" for o in pending)
    account.buy(DAYS[1], "A", 1000, 1, "probe")
    pending, _ = orders({"A": 0.05, "B": 0.10}, 25000, 1000)
    assert not pending
    with pytest.raises(ValueError):
        op.prepare_orders(account, {}, prices, 0, plan, isolated_reserve=True)


def test_unfundable_core_buys_do_not_occupy_the_trial_slot_or_block_real_sales():
    core, held = {"A": 0.10, "B": 0.20, "C": 0.04}, {"B": 0.12, "C": 0.13, "D": 0.06}
    assert op.executable_core_targets(core, held, 100000, 55500, 1000) == core
    actual = op.executable_core_targets(core, held, 100000, 56000, 1000)
    assert actual == {"B": 0.12, "C": 0.04}
    targets, plan = op.apply_probe(
        actual,
        held,
        [candidate()],
        navs(),
        {"A": 0},
        closed_sessions=DAYS[:21],
        day=DAYS[21],
        cash_weight=0.5,
    )
    assert targets["A"] == 0.03 and plan["action"] == "enter"
    assert "D" not in targets and targets["C"] == 0.04
    assert core["A"] == 0.10
