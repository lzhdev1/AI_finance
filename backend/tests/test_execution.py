import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from quantlab import db, execution, pod_store, runner, sources
from quantlab.config import DB_DIR
from quantlab.pods import Fill, PodAccount


def test_freezes_amount_and_units_before_close_without_reusing_redemption():
    account = PodAccount(10_000, 10_000)
    account.buy(date(2026, 1, 5), "A", 8_000, 1.0)
    orders, notes = execution.prepare_orders(account, {"B": 0.9}, {"A": 1, "B": 1}, 20_000)
    assert notes and [(o["code"], o["amount"]) for o in orders] == [("A", 8_000), ("B", 2_000)]
    fills = execution.fill_orders(account, date(2026, 1, 6), orders, {"A": 1.1, "B": 1.2}, "")
    assert fills[0].shares == 8_000
    assert fills[0].amount == 8_800
    assert fills[1].amount == 2_000
    assert fills[1].shares == pytest.approx(2_000 / 1.2)
    assert account.cash == pytest.approx(8_800 - fills[0].fee)


def test_historical_recorded_fills_are_independent_of_current_fee_rates():
    account = PodAccount(10_000, 10_000, fees={"A": {"buy": 0.3, "sell": [[999, 0.5]]}})
    account.apply_fill(Fill(date(2026, 1, 5), "A", "buy", 3_000, 1_499, 2, 2))
    account.apply_fill(Fill(date(2026, 1, 6), "A", "sell", 2_200, 1_000, 2.2, 15))
    assert account.cash == 9_185
    assert account.shares_of("A") == 499
    assert account.average_cost("A") == pytest.approx(3_000 / 1_499)
    assert execution.prepare_orders(account, {}, {}, 0)[0] == []
    assert execution.prepare_orders(account, {}, {"A": 1}, 0)[0][0]["shares"] == 499


def test_risk_preserves_cash_and_caps_common_board():
    rules = SimpleNamespace(max_position=0.35, max_board_exposure=0.45)
    funds = {c: {"kind": "otc", "is_active": True, "role": "risk"} for c in ["A", "B"]}
    targets, checks = execution.risk_targets(
        {"A": 0.50, "B": 0.30}, funds, {"A": {"科技": 1}, "B": {"科技": 1}}, rules
    )
    assert sum(targets.values()) == pytest.approx(0.45)
    assert targets["A"] <= 0.35
    assert len(checks) == 2
    assert execution.risk_targets({"A": 0.20}, funds, {}, rules)[0] == {"A": 0.20}
    with pytest.raises(ValueError):
        execution.risk_targets({"A": float("nan")}, funds, {}, rules)


def test_nav_feed_fallback_preserves_publication_date_and_source(monkeypatch):
    import requests

    def get(url, **kwargs):
        if "/f10/" in url:
            raise requests.ReadTimeout()
        script = (
            'var Data_netWorthTrend = [{"x":1767628800000,"y":1.1}];'
            "var Data_ACWorthTrend = [[1767628800000,1.25]];"
        )
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, text=script)

    monkeypatch.setattr(sources.THROTTLE, "wait", lambda: None)
    monkeypatch.setattr(requests, "get", get)
    frame = sources.fetch_fund_nav("000001", since=date(2026, 1, 6))
    assert frame.to_dict("records") == [
        {"trade_date": date(2026, 1, 6), "nav": 1.1, "acc_nav": 1.25}
    ]
    assert frame.attrs["source"] == "eastmoney-pingzhong"


@pytest.fixture
def ledger(monkeypatch):
    if os.getenv("QUANTLAB_TEST_POSTGRES") != "1":
        pytest.skip("set QUANTLAB_TEST_POSTGRES=1 to run the isolated PostgreSQL ledger test")
    schema = "ledger_test_" + uuid4().hex
    monkeypatch.setattr(db, "_schema", schema)
    with db.transaction() as conn:
        conn.execute(
            (DB_DIR / "schema.sql").read_text(encoding="utf-8").replace("quantlab", schema)
        )
    try:
        db.execute(
            f"INSERT INTO {schema}.trade_calendar(trade_date) VALUES ('2026-01-05'), ('2026-01-06'), ('2026-01-07'), ('2026-01-08')"
        )
        asset = db.query_one(
            f"INSERT INTO {schema}.instrument(code, name, kind) VALUES ('000001','示例','otc') RETURNING instrument_id"
        )
        pod_id = pod_store.create_pod(
            name="测试",
            strategy_key="agent_manager",
            thesis="模拟",
            invalidation_rules=[],
            entry_evidence={},
            trade_date=date(2026, 1, 6),
        )
        yield pod_store.get_pod(pod_id), asset["instrument_id"]
    finally:
        db.execute(f"DROP SCHEMA {schema} CASCADE")


def test_delayed_nav_atomic_retry_and_full_redemption(ledger, monkeypatch):
    pod, asset_id = ledger
    prev, day, tomorrow = (date(2026, 1, n) for n in (5, 6, 7))

    def price(d, nav):
        db.execute(
            f"INSERT INTO {db.qualified('instrument_nav')} (instrument_id, trade_date, nav) VALUES (%s,%s,%s)",
            [asset_id, d, nav],
        )

    def plan(d, weights):
        return runner.plan_pod(pod, d, {}, {"targets": weights, "id": 8}, replay=True)

    price(prev, 1)
    first = plan(day, {"000001": 0.30})
    assert first["pending"][0]["amount"] == 30_000
    assert plan(day, {})["pending"] == first["pending"]
    assert runner.execute_pod(pod, day)["status"] == "waiting_nav"
    assert plan(tomorrow, {})["status"] == "blocked"
    price(day, 2)
    original = pod_store.save_snapshot

    def fail_after_fills(*args):
        raise RuntimeError("模拟崩溃")

    monkeypatch.setattr(pod_store, "save_snapshot", fail_after_fills)
    with pytest.raises(RuntimeError):
        runner.execute_pod(pod, day)
    assert db.query_one(f"SELECT count(*) n FROM {db.qualified('pod_order')}")["n"] == 0
    monkeypatch.setattr(pod_store, "save_snapshot", original)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: runner.execute_pod(pod, day)["status"], [0, 1]))
    assert sorted(results) == ["already_settled", "settled"]
    acc = pod_store.load_account(pod)
    assert acc.shares_of("000001") == 15_000
    assert acc.cash == 70_000
    assert plan(tomorrow, {})["pending"][0]["shares"] == 15_000
    price(tomorrow, 1.1)
    outcome = runner.execute_pod(pod, tomorrow)
    assert outcome["snapshot"]["cash"] == pytest.approx(70_000 + 16_500 * 0.985)
    assert pod_store.positions(pod["pod_id"]).empty
    assert not pod_store.load_account(pod).codes()
    hold = plan(date(2026, 1, 8), {"000001": 0.2})
    assert hold["payload"]["targets"] == {}
    assert "止损" in hold["rationale"]


def test_nav_valuations_only_for_closed_dates_without_unsettled_orders(ledger):
    pod, asset = ledger
    start, next_day, pending_day = (date(2026, 1, d) for d in (6, 7, 8))
    db.execute(
        f"""INSERT INTO {db.qualified("pod_order")}
        (pod_id,decided_on,execute_on,instrument_id,side,amount,shares,nav,fee)
        VALUES (%s,%s,%s,%s,'buy',1000,1000,1,0)""",
        [pod["pod_id"], start, start, asset],
    )
    db.execute(
        f"INSERT INTO {db.qualified('instrument_nav')} (instrument_id,trade_date,nav) VALUES (%s,%s,1)",
        [asset, start],
    )
    first = execution.mark_to_market(pod, pending_day)
    assert first == {
        "valued": [str(start)],
        "waiting_date": str(next_day),
        "missing_nav": ["000001"],
    }
    db.execute(
        f"INSERT INTO {db.qualified('instrument_nav')} (instrument_id,trade_date,nav) VALUES (%s,%s,2)",
        [asset, next_day],
    )
    runner.plan_pod(pod, pending_day, {}, {"targets": None}, replay=True)
    assert execution.mark_to_market(pod, pending_day) == {"valued": [str(next_day)]}
    assert execution.mark_to_market(pod, pending_day) == {"valued": []}
    assert pod_store.nav_history(pod["pod_id"]).iloc[-1]["total_value"] == 101_000


def test_manager_intraday_freeze_honors_observe_on_strong_fund(ledger):
    from quantlab import research

    pod, asset = ledger
    previous, day = date(2026, 1, 5), date(2026, 1, 6)
    db.execute(
        f"INSERT INTO {db.qualified('instrument_nav')} (instrument_id, trade_date, nav) VALUES (%s,%s,1)",
        [asset, previous],
    )
    watch = [
        {
            "board": "保险",
            "intent": "observe",
            "funds": [{"code": "000001"}],
            "momentum_5": 0.1,
            "momentum_20": 0.1,
            "forecast_factor": 1.0,
            "memory_factor": 1.0,
        }
    ]
    targets = research._targets(watch, "reduced")
    report = {
        "id": 8,
        "targets": targets,
        "rationale": research._allocation_summary(targets, watch),
    }
    card = runner.plan_pod(pod, day, {}, report, replay=True)
    assert card["payload"]["targets"] == {} and card["pending"] == []
    assert "仅观察保险" in card["rationale"]
