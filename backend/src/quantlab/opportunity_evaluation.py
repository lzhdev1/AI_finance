"""Paired, read-only replay of the opportunity sleeve against the same archived research."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import pandas as pd

from . import db, execution, manager_v2, pod_store, replay_archive
from .config import CONFIG_DIR, DATA_DIR, load_settings
from .pods import PodAccount
from .replay_universe import terms_on

SH = ZoneInfo("Asia/Shanghai")
HOLDOUT_START = date(2026, 9, 21)


@dataclass
class Ledger:
    account: PodAccount
    previous: dict | None = None
    monthly_turnover: Counter = field(default_factory=Counter)
    monthly_probes: Counter = field(default_factory=Counter)
    withdrawals: list = field(default_factory=list)
    peak: float = 0
    drawdown: float = 0
    state: dict = field(default_factory=dict)
    records: list = field(default_factory=list)


def read_source():
    """Only the sealed V2 paper account is an admissible source for this comparison."""
    t = db.qualified
    account = db.query_one(f"""SELECT current_database() AS database, initial_capital, entry_evidence
        FROM {t("pod")} WHERE strategy_key='agent_manager'""")
    proof = account["entry_evidence"] if account else {}
    if (
        proof.get("mode") != "historical_replay"
        or proof.get("strategy_version") != "v2"
        or not account["database"].startswith("quantlab_replay_")
        or account["database"] != proof.get("run_id")
    ):
        raise ValueError("需要已封存的独立 V2 历史回放库")
    start, end = date.fromisoformat(proof["start"]), date.fromisoformat(proof["end"])
    sessions = [
        r["trade_date"]
        for r in db.query(
            f"""SELECT trade_date FROM {t("trade_calendar")}
        WHERE trade_date<=%s ORDER BY trade_date""",
            [end + timedelta(days=35)],
        )
    ]
    reports = db.query(f"""SELECT r.report_id, r.as_of, r.payload->'targets' AS targets,
        r.payload->>'rationale' AS rationale,
        r.payload->'evidence'->'fund_features' AS features, r.payload->>'stance' AS stance,
        r.mode, d.trade_date AS day
        FROM {t("pod_decision")} d JOIN {t("pod")} p USING(pod_id)
        JOIN {t("agent_research")} r ON r.report_id=(d.payload->>'research_id')::bigint
        WHERE p.strategy_key='agent_manager' AND r.payload->>'strategy_version'='v2'
        AND d.executed_at IS NOT NULL ORDER BY d.trade_date""")
    if [r["day"] for r in reports] != [d for d in sessions if start <= d <= end]:
        raise ValueError("V2 已确认的决策/研究记录无法覆盖完整区间")
    if any(
        r["as_of"].astimezone(SH).date() != r["day"]
        or r["as_of"].astimezone(SH) > datetime.combine(r["day"], time(14, 30), SH)
        for r in reports
    ):
        raise ValueError("缺少当日决策截止前的归档研究")
    instruments = pod_store.instrument_map(include_inactive=True)
    mapping = db.query(f"""SELECT i.code, b.board_id, b.name AS board, ib.weight::float AS weight,
        ib.source mapping_source, i.listed_on, b.is_active board_active
        FROM {t("instrument_board")} ib JOIN {t("instrument")} i USING(instrument_id)
        JOIN {t("board")} b USING(board_id) WHERE ib.weight>0 ORDER BY b.board_id,i.code""")
    boards = defaultdict(dict)
    for row in mapping:
        boards[row["code"]][row["board"]] = min(1.0, row["weight"])
    bars = defaultdict(list)
    for r in db.query(
        f"""SELECT q.board_id, q.trade_date, q.close::float, q.high::float,
        q.low::float, q.open::float, q.amount::float, q.volume::float, q.source, q.ingested_at,
        x.pct_chg::float AS benchmark_pct FROM {t("board_quote")} q
        JOIN {t("board")} b ON b.board_id=q.board_id AND b.source=q.source
        LEFT JOIN {t("index_quote")} x ON x.index_code='000300' AND x.trade_date=q.trade_date
        WHERE q.board_id=ANY(%s) AND q.trade_date<=%s ORDER BY q.board_id,q.trade_date""",
        [sorted({r["board_id"] for r in mapping}), end],
    ):
        bars[r["board_id"]].append(r)
    nav_days = defaultdict(dict)
    for r in db.query(
        f"""SELECT i.code,n.trade_date,n.nav::float FROM {t("instrument_nav")} n
        JOIN {t("instrument")} i USING(instrument_id) WHERE n.trade_date<=%s AND n.nav>0
        ORDER BY n.trade_date,i.code""",
        [end],
    ):
        nav_days[r["trade_date"]][r["code"]] = r["nav"]
    benchmark = {
        r["trade_date"]: r["close"]
        for r in db.query(
            f"""SELECT trade_date,close::float
        FROM {t("index_quote")} WHERE index_code='000300' AND close>0 AND trade_date<=%s""",
            [end],
        )
    }
    disclosures = json.loads(
        (CONFIG_DIR / "replay_fund_disclosures.json").read_text(encoding="utf-8")
    )
    fees = pod_store.fee_book(list(instruments))
    recorded_codes = db.query(f"""SELECT i.code FROM {t("instrument_fee")} f
        JOIN {t("instrument")} i USING(instrument_id) GROUP BY i.code
        HAVING count(DISTINCT f.side)=2""")
    return dict(
        source=account["database"],
        initial=float(account["initial_capital"]),
        start=start,
        end=end,
        disclosures={f["code"]: f for f in disclosures["funds"] if f["document_confirms_index"]},
        sessions=sessions,
        reports=reports,
        instruments=instruments,
        mapping=mapping,
        boards=boards,
        bars=bars,
        nav_days=nav_days,
        benchmark=benchmark,
        fees=fees,
        recorded_fees={r["code"]: fees[r["code"]] for r in recorded_codes},
    )


def _timing(book, sessions, index):
    # Live simulator defaults to T+2 redeemability. The confirmed bank-fund
    # prospectus releases its redemption proceeds after seven whole sessions.
    available_on = sessions[index - 2] if index >= 2 else date.min
    redeemable = {
        code: sum(lot.shares for lot in lots if lot.buy_date <= available_on)
        for code, lots in book.account.lots.items()
    }
    waiting = sum(amount for released, amount in book.withdrawals if index < released)
    return {"redeemable": redeemable, "cash_receivable": waiting}


def _performance(records, before, first, last):
    selected = [r for r in records if first <= r["date"] <= last]
    peak = before
    drawdown = 0
    for row in selected:
        peak = max(peak, row["value"])
        drawdown = min(drawdown, row["value"] / peak - 1)
    fills = [f for row in selected for f in row["orders"]]
    return {
        "return": selected[-1]["value"] / before - 1,
        "max_drawdown": drawdown,
        "fees": round(sum(f["fee"] for f in fills), 4),
        "buy_count": sum(f["side"] == "buy" for f in fills),
        "sell_count": sum(f["side"] == "sell" for f in fills),
        "exposure_days": sum(r["invested"] > 0 for r in selected),
    }


def _fifo_basis(lots, shares):
    cost = 0.0
    for lot in lots:
        take = min(shares, lot.shares)
        cost += lot.unit_cost * take
        shares -= take
        if shares < 1e-7:
            return cost
    raise ValueError("拟赎回份额超出已确认的持仓份额")


def _forward_checks(buys, sessions, nav_days, end):
    """Only after all decisions are sealed, score direction from decision-day NAV to T+h."""
    index = {d: i for i, d in enumerate(sessions)}
    rows = []
    for h in (1, 5):
        observed = []
        for buy in buys:
            start = date.fromisoformat(buy["date"])
            j = index[start] + h
            target = sessions[j] if j < len(sessions) else None
            nav = nav_days.get(target, {}).get(buy["code"]) if target and target <= end else None
            value = nav / buy["nav"] - 1 if nav else None
            buy[f"return_{h}d"] = value
            if value is not None:
                observed.append(value)
        hits = sum(r > 0 for r in observed)
        rows.append(
            {
                "horizon_days": h,
                "total": len(buys),
                "scored": len(observed),
                "hits": hits,
                "misses": len(observed) - hits,
                "unresolved": len(buys) - len(observed),
                "worst_return": min(observed, default=None),
                "hit_rate": hits / len(observed) if observed else None,
                "average_return": mean(observed) if observed else None,
            }
        )
    return rows


def _session_inputs(source, report, index, nav_history):
    from . import opportunity

    day = report["day"]
    # The model receives physically sliced prefixes; future rows alone cannot
    # influence eligibility, fund selection, fees, or risk exits.
    closed = source["sessions"][max(0, index - 220) : index]
    navs = {code: series.loc[: closed[-1]] for code, series in nav_history.items()}
    catalog = {code: dict(info) for code, info in source["instruments"].items()}
    for term in terms_on(day):
        if term["code"] in catalog:
            catalog[term["code"]].update(term)
    groups = {}
    for mapping in source["mapping"]:
        code = mapping["code"]
        doc = source["disclosures"].get(code)
        if (
            not doc
            or doc["disclosed"] > str(day)
            or doc["mapped_sector"] != mapping["board"]
            or mapping["listed_on"]
            and mapping["listed_on"] > day
            or not mapping["board_active"]
            or catalog[code]["kind"] != "otc"
            or not catalog[code]["is_active"]
            or navs[code].empty
            or closed[-1] not in navs[code].index
        ):
            continue
        group = groups.setdefault(mapping["board_id"], {"board": mapping["board"], "funds": []})
        group["funds"].append(
            {
                "code": code,
                "name": catalog[code]["name"],
                "mapping_source": mapping["mapping_source"],
                "disclosure": doc["source_url"],
            }
        )
    signals = []
    for board_id, group in groups.items():
        bars = [
            row for row in source["bars"].get(board_id, []) if closed[0] <= row["trade_date"] < day
        ]
        signal = opportunity.assess(
            bars, closed_sessions=closed, as_of=report["as_of"], replay=True
        )
        signals.append(dict(signal, board_id=board_id, **group))
    future = source["sessions"][index + 1 : index + 6]
    fee_book = opportunity.disclosed_fees(source["recorded_fees"], list(catalog), day)
    costs = {
        code: opportunity.fee_cost(fee, day, future[-1]) if len(future) == 5 else None
        for code, fee in fee_book.items()
    }
    return closed, navs, catalog, signals, costs, fee_book


def _step(book, treatment, source, report, index, inputs):
    from . import opportunity

    day = report["day"]
    rules = load_settings().agent
    month = (day.year, day.month)
    closed, navs, catalog, signals, costs, fee_book = inputs
    book.account.fees = {
        **source["fees"],
        **{c: f for c, f in fee_book.items() if costs[c] is not None},
    }
    previous_day = closed[-1]
    prices = source["nav_days"][previous_day]
    timing = _timing(book, source["sessions"], index)
    available = book.account.cash - timing["cash_receivable"]
    equity = book.account.total_value(prices)
    held = book.account.weights(prices)
    hard_stop = book.drawdown <= -abs(rules.pod_hard_stop_drawdown)
    proposal = {} if hard_stop else report["targets"]
    original = None
    if proposal is None:
        core = held
    else:
        original, _ = execution.risk_targets(proposal, catalog, source["boards"], rules)
        core, _ = manager_v2.execution_targets(
            book.account,
            prices,
            day,
            original,
            book.previous,
            report["features"] or {},
            hard_stop=hard_stop,
        )
        core, _ = execution.risk_targets(core, catalog, source["boards"], rules)
    book.previous = original
    core_targets = core.copy()
    core_reason = (
        "组合触及硬止损，申请转为现金。"
        if hard_stop
        else report.get("rationale") or "归档 V2 核心目标及资金、持仓约束"
    )
    budget = max(0.0, source["initial"] * rules.max_monthly_turnover - book.monthly_turnover[month])
    if treatment == "executable":
        core = opportunity.executable_core_targets(
            core, held, source["initial"], book.monthly_turnover[month], book.monthly_probes[month]
        )
    executable_core = core.copy()
    if treatment != "control":
        core, plan = opportunity.apply_probe(
            core,
            held,
            signals,
            navs,
            costs,
            closed_sessions=closed,
            day=day,
            previous=book.state or None,
            cash_weight=available / equity,
            allow_new=proposal is not None and report["stance"] != "cash" and not hard_stop,
            force_exit=hard_stop,
        )
        core, _ = execution.risk_targets(core, catalog, source["boards"], rules)
        pending, notes = opportunity.prepare_orders(
            book.account,
            core,
            prices,
            budget,
            plan,
            month_probe_buys=book.monthly_probes[month],
            isolated_reserve=treatment in ("independent", "executable"),
            total_month_turnover=book.monthly_turnover[month],
            **timing,
        )
    else:
        plan = {}
        pending, notes = execution.prepare_orders(book.account, core, prices, budget, **timing)
    # The decision is now frozen. Only settlement and subsequent measurement
    # can see the session's published closing NAV.
    actual = source["nav_days"][day]
    required = set(book.account.codes()) | {o["code"] for o in pending}
    if required - set(actual):
        raise ValueError(f"{day} 缺少模拟申赎/估值净值 {sorted(required - set(actual))}")
    bases = {
        p["code"]: _fifo_basis(book.account.lots[p["code"]], p["shares"])
        for p in pending
        if p["side"] == "sell"
    }
    fills = execution.fill_orders(book.account, day, pending, actual, "按冻结指令结算", **timing)
    orders = []
    for fill in fills:
        attributed = plan.get("code") == fill.code and (plan.get("action"), fill.side) in {
            ("enter", "buy"),
            ("exit", "sell"),
        }
        if attributed and fill.side == "buy":
            book.monthly_probes[month] += fill.amount
        book.monthly_turnover[month] += fill.amount
        if fill.side == "sell" and fill.code == "014028":
            book.withdrawals.append((index + 8, fill.amount - fill.fee))
        orders.append(
            {
                "date": str(day),
                "side": fill.side,
                "code": fill.code,
                "name": catalog[fill.code]["name"],
                "amount": fill.amount,
                "shares": fill.shares,
                "nav": fill.nav,
                "fee": fill.fee,
                "fee_basis": "费率已有记录" if costs.get(fill.code) is not None else "模拟默认档位",
                "fee_document": fee_book.get(fill.code, {}).get("source"),
                "realized_net_pnl": round(fill.amount - fill.fee - bases[fill.code], 4)
                if fill.side == "sell"
                else None,
                "is_probe": attributed,
                "state": plan.get("state") if attributed else None,
                "board": plan.get("board") if attributed else None,
                "reason": plan.get("reason") if attributed else core_reason,
            }
        )
    if treatment != "control":
        book.state = plan
    value = book.account.total_value(actual)
    book.peak = max(book.peak, value)
    book.drawdown = value / book.peak - 1
    result = {
        "date": str(day),
        "value": value,
        "cash": book.account.cash,
        "invested": book.account.market_value(actual),
        "orders": orders,
        "turnover_left_before": round(budget, 2),
        "spendable_cash_before": round(available, 4),
        "core_targets": core_targets,
        "executable_core_targets": executable_core,
        "holdings_before": held,
        "execution_notes": notes,
    }
    if treatment != "control":
        result["plan"] = plan
    book.records.append(result)
    return result


def evaluate(source):
    initial = source["initial"]
    books = {
        policy: Ledger(
            PodAccount(
                initial,
                initial,
                fees=source["fees"],
                min_trade_amount=load_settings().min_trade_amount,
            ),
            peak=initial,
        )
        for policy in ("control", "shared", "independent", "executable")
    }
    nav_history = pd.DataFrame.from_dict(source["nav_days"], orient="index").sort_index()
    navs = {code: nav_history[code].dropna() for code in source["instruments"]}
    sessions = source["sessions"]
    indexes = {d: i for i, d in enumerate(sessions)}
    base = source["benchmark"][sessions[indexes[source["start"]] - 1]]
    days = []
    for report in source["reports"]:
        day = report["day"]
        index = indexes[day]
        inputs = _session_inputs(source, report, index, navs)
        records = {
            policy: _step(book, policy, source, report, index, inputs)
            for policy, book in books.items()
        }
        days.append(
            {
                "date": str(day),
                "report_id": report["report_id"],
                "information_cutoff": report["as_of"].astimezone(SH).isoformat(),
                "market_through": str(sessions[index - 1]),
                "baseline_value": records["control"]["value"],
                "opportunity_value": records["executable"]["value"],
                "benchmark_value": initial * source["benchmark"][day] / base,
                "signals": [
                    {
                        "board_id": s["board_id"],
                        "board": s["board"],
                        "state": s["state"],
                        "score": s["score"],
                        "evidence": s.get("evidence"),
                        "invalidation": s.get("invalidation"),
                        "funds": [
                            {**fund, "round_trip_fee": inputs[4].get(fund["code"])}
                            for fund in s["funds"]
                        ],
                    }
                    for s in inputs[3]
                    if s.get("eligible")
                ],
                "states": dict(Counter(s["state"] for s in inputs[3])),
                "baseline": records["control"],
                "shared_reserve": records["shared"],
                "previous_independent": records["independent"],
                "opportunity": records["executable"],
            }
        )
    segments = {policy: [] for policy in ("shared", "independent", "executable")}
    start, end = source["start"], source["end"]
    for k, label, first, last in [
        ("full", "全部区间", start, end),
        ("june", "六月", date(2026, 6, 1), date(2026, 6, 30)),
        ("holdout", "最后两个月", HOLDOUT_START, end),
    ]:
        dates = [d for d in sessions if max(start, first) <= d <= min(last, end)]
        if not dates:
            continue
        first, last = dates[0], dates[-1]
        prev = sessions[indexes[first] - 1]
        index_peak = source["benchmark"][prev]
        index_drawdown = 0.0
        for day in dates:
            close = source["benchmark"][day]
            index_peak = max(index_peak, close)
            index_drawdown = min(index_drawdown, close / index_peak - 1)
        results = {}
        for policy, book in books.items():
            before = next((r["value"] for r in book.records if r["date"] == str(prev)), initial)
            results[policy] = _performance(book.records, before, str(first), str(last))
        for policy, output in segments.items():
            output.append(
                {
                    "key": k,
                    "label": label,
                    "start": str(first),
                    "end": str(last),
                    "index_return": source["benchmark"][last] / source["benchmark"][prev] - 1,
                    "index_max_drawdown": index_drawdown,
                    "baseline": results["control"],
                    "opportunity": results[policy],
                    "excess_vs_control": results[policy]["return"] - results["control"]["return"],
                }
            )
    interventions = {}
    for policy in segments:
        trades = [r for d in books[policy].records for r in d["orders"] if r["is_probe"]]
        forward = _forward_checks(
            [r for r in trades if r["side"] == "buy"], sessions, source["nav_days"], end
        )
        interventions[policy] = {
            "segments": segments[policy],
            "opportunity_trades": trades,
            "forward_checks": forward,
        }
    return {
        "status": "completed",
        "source": source["source"],
        "period": {
            "start": str(start),
            "end": str(end),
            "holdout_start": str(HOLDOUT_START),
            "trade_days": len(days),
        },
        "initial_capital": initial,
        "accounts": [
            "frozen-v2-control",
            "frozen-v2-shared-reserve",
            "frozen-v2-independent-reserve",
            "frozen-v2-executable-core",
        ],
        "method": "四个独立模拟账本从等额现金开始，复用 V2 每日14:30已经冻结的研究目标。"
        "三种试仓方案共用信号、仓位和退出规则。首轮从总换手额内预留5%，第二轮将5%的试仓申购预算单列，"
        "余下55%供核心交易和赎回使用。第三轮只调整执行顺序，核心买入额度耗尽时，先剔除不能成交的核心加仓，再选试仓。"
        "所有账本优先允许风险赎回，实际双边换手可超过60%。"
        "按上一交易日行情冻结申购金额/赎回份额，以当日真实基金净值、FIFO费率及同一模拟申赎时钟结算。",
        "limitations": [
            "历史14:30决策复用归档观点，模型没有按替代账户重新研究。四个账户均使用同一套新申赎时钟，"
            "对照账户的结果可能与旧V2原账不同。",
            "价格、量额和净值为回填的历史日线，历史发布版本难以复原。"
            "回放使用截至前一交易日的日线与净值；该范围没有历史盘中快照或可核验的当时资金流。",
            "基金池、简称匹配及费率依赖现有历史归档和已核实的变更，可能存在幸存者偏差。"
            "CPO 没有可交易的直接映射。",
            "核心目标去重的执行问题来自六月复盘；六月的改善属于样本内结果。最后两个月仅为时序分段，同一历史区间已经被观察。"
            "试探入场后的标的净值方向统计不含费用，账户收益已计费。",
            "机会交易只使用有记录的申赎费率。核心交易的缺档费用沿用模拟账本默认费率，各笔标明依据。"
            "板块方向预测沿用原归档，试探仓次日及五日统计仅覆盖实际试探申购。",
        ],
        **interventions["executable"],
        "previous_reserve": interventions["shared"],
        "previous_independent": interventions["independent"],
        "days": days,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DATA_DIR / "manager-replay" / "opportunity-evaluation.json"
    )
    args = parser.parse_args()
    with db.transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        source = read_source()
    archive = evaluate(source)
    replay_archive.write_snapshot(args.output, archive)
    for segment in archive["segments"]:
        print(segment["key"], segment["baseline"], segment["opportunity"], flush=True)
    print(
        "opportunity trades",
        len(archive["opportunity_trades"]),
        archive["forward_checks"],
        flush=True,
    )


if __name__ == "__main__":
    main()
