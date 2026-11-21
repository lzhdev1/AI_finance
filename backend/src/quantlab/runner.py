"""每日流水线编排：抓数据之后，让 agent 走完"决策 → 结算 → 考核 → 评选"一遍。

顺序很重要：
1. 先给所有存活仓出决策卡（含预测）；
2. 再结算昨天的预测（必须今天才结算，避免用未来数据）；
3. 然后考核（止损 / 到期淘汰），决定谁留谁走；
4. 最后考虑是否开新仓，并评选"今日最看好"。

先决策后考核是有意的：考核用的是**已经实现**的绩效，决策用的是**当天收盘**的数据，
两者不共享未来信息。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from . import db, decide, execution, manager_v2, opportunity, opportunity_research, otc, pod_store
from .config import load_settings, load_universe
from .pod_store import assert_legacy_replay_database as assert_replay_database
from .pods import board_overlap

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 交易日

def is_trade_day(day: date) -> bool:
    row = db.query_one(
        f"SELECT 1 AS ok FROM {db.qualified('trade_calendar')} WHERE trade_date = %s", [day]
    )
    return row is not None


def latest_trade_date(on_or_before: date | None = None) -> date | None:
    row = db.query_one(
        f"SELECT max(trade_date) AS d FROM {db.qualified('trade_calendar')} "
        "WHERE trade_date <= COALESCE(%s, current_date)",
        [on_or_before],
    )
    return row["d"] if row else None


def trade_days_between(start: date, end: date) -> int:
    row = db.query_one(
        f"SELECT count(*) AS n FROM {db.qualified('trade_calendar')} "
        "WHERE trade_date > %s AND trade_date <= %s",
        [start, end],
    )
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- 单仓决策

def monthly_turnover_left(pod: Mapping, trade_date: date) -> float:
    """本月剩余的可成交金额（按双边成交额计）。"""
    return execution.monthly_turnover_left(pod, trade_date)


def prev_trade_date(trade_date: date) -> date | None:
    row = db.query_one(
        f"SELECT max(trade_date) AS d FROM {db.qualified('trade_calendar')} WHERE trade_date < %s",
        [trade_date],
    )
    return row["d"] if row and row["d"] else None


def _last_snapshot(pod_id: int) -> dict:
    row = db.query_one(
        f"SELECT * FROM {db.qualified('pod_nav')} WHERE pod_id = %s ORDER BY trade_date DESC LIMIT 1",
        [pod_id],
    )
    if not row:
        return {
            "unit_nav": 1.0, "cum_return": 0.0, "benchmark_return": None,
            "excess_return": None, "drawdown": 0.0, "cash": 0.0,
        }
    return row


def _decision_barrier(pod_id: int, day: date) -> str | None:
    older = db.query_one(
        f"SELECT min(trade_date) AS day FROM {db.qualified('pod_decision')} "
        "WHERE pod_id = %s AND trade_date < %s AND executed_at IS NULL",
        [pod_id, day],
    )
    if older["day"]:
        return f"{older['day']} 指令尚未完成净值确认"
    later = db.query_one(
        f"SELECT 1 AS found FROM {db.qualified('pod_nav')} "
        "WHERE pod_id = %s AND trade_date > %s LIMIT 1", [pod_id, day],
    )
    return "该日之后已有确认估值，不能回填历史指令" if later else None


def _missed_cutoff(day: date) -> bool:
    from . import station

    now = station.now()
    start = load_settings().schedule["intraday_decide"]
    return day != now.date() or not start <= now.strftime("%H:%M") < "15:00"


def _planned_action(account, instructions):
    held = account.codes()
    if not instructions:
        return "hold" if held else "cash"
    sides = {order["side"] for order in instructions}
    if sides == {"buy"}:
        return "add" if held else "open"
    if sides == {"sell"}:
        sold = {order["code"]: order["shares"] for order in instructions}
        liquidated = all(sold.get(code, 0) >= account.shares_of(code) - 0.0000005 for code in held)
        return "close" if liquidated else "reduce"
    return "rebalance"


def _publication(day, source, reason, report, reviewed_through, published, instructions=()):
    from . import station
    window = report.get("forecast_window") or {}
    if window:
        targets = {"1": window.get("next_session"), "5": window.get("fifth_session")}
    else:
        days = otc.sessions(day, include_start=False, count=5)
        targets = {str(n): str(days[n-1]) if len(days) >= n else None for n in (1, 5)}
    codes = {order["code"] for order in instructions}
    draft = source == "prepared_review"
    forecasts = report.get("fund_estimates" if draft else "fund_forecasts") or []
    outlooks = []
    for row in forecasts:
        horizon = str(row.get("horizon_days"))
        if (row.get("code") in codes and horizon in targets and targets[horizon]
                and str(row.get("target_date")) == targets[horizon] and row.get("prob_up") is not None):
            fields = ("code", "name", "horizon_days", "target_date", "prob_up", "expected_return", "status")
            entry = {key: row.get(key) for key in fields}
            if draft:
                entry["status"] = "estimate"
            outlooks.append(entry)
    return {
        "published_at": published.isoformat(), "evidence_as_of": report.get("as_of"),
        "reviewed_through": reviewed_through.isoformat() if reviewed_through else None,
        "source": source, "reason": reason, "forecast_targets": targets,
        "late": published > station.scheduled_time(day, "decision_publish_by"),
        "inputs": report.get("decision_inputs", {}),
        "fund_outlooks": outlooks,
    }


def _unreviewed_reason(day, report, source, stamp):
    from . import station
    start = station.scheduled_time(day, "decision_prepare" if source == "prepared_review" else "intraday_decide")
    if report.get("run_mode") != "live" or not report.get("as_of") or not start <= datetime.fromisoformat(report["as_of"]) <= stamp:
        return "未取得当日发布窗口的有效研究"
    if report.get("mode") not in ("rules", "llm") or report.get("targets") is None:
        return "本轮研究可用数据不足，未形成可执行的目标仓位"
    window = report.get("forecast_window") or {}
    if window.get("base_date") != str(day) or not all(window.get(k) for k in ("next_session", "fifth_session")):
        return "交易日历不足以判断本次指令的下一、第五交易日预测目标"
    inputs = report.get("decision_inputs", {})
    if not inputs.get("snapshot", {}).get("boards"):
        return "本轮收盘前刷新未取得可核验板块快照"
    news = inputs.get("news", {})
    if not any(isinstance(info, dict) and info.get("rows") for info in news.values()):
        return "本轮收盘前刷新未取得可核验的新闻源响应"
    return None


def plan_pod(pod: Mapping, market: decide.MarketView | date, universe: dict,
             research_report: Mapping | None = None, *, replay: bool = False, publication_source=None) -> dict:
    """Lock the book and freeze the actual subscription amount and redemption units."""
    manager = pod["strategy_key"] == "agent_manager"
    if isinstance(market, date) and not manager:
        raise ValueError("策略回放需要包含行情的 MarketView")
    day = market if isinstance(market, date) else market.trade_date
    pod_id = pod["pod_id"]
    with db.transaction() as conn:
        if publication_source:
            conn.execute("SET LOCAL lock_timeout='2s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
        current = db.query_one(f"SELECT status FROM {db.qualified('pod')} WHERE pod_id = %s FOR UPDATE", [pod_id])
        existing = db.query_one(
            f"SELECT * FROM {db.qualified('pod_decision')} WHERE pod_id = %s AND trade_date = %s",
            [pod_id, day],
        )
        if existing:
            return existing | {"status": "existing", "pending": (existing["payload"] or {}).get("pending", [])}
        if not current or current["status"] not in ("active", "observing") or not is_trade_day(day) or day < pod["opened_on"]:
            return {"action": "hold", "status": "blocked", "summary": "当前账户或日期不满足交易条件"}
        if not replay and _missed_cutoff(day):
            return {"action": "hold", "status": "cutoff", "summary": "当前不在当日模拟申赎指令窗口"}
        barrier = _decision_barrier(pod_id, day)
        if barrier:
            return {"action": "hold", "status": "blocked", "summary": barrier}

        prev = prev_trade_date(day)
        account = pod_store.load_account(pod, as_of=day)
        instruments = pod_store.instrument_map(include_inactive=True)
        codes = {code for code, item in instruments.items() if item["is_active"]} | set(account.codes())
        if replay:
            prices = execution.nav_on(prev, codes)
        else:
            from . import station
            prices = execution.nav_on(prev, codes, visible_by=station.now())
        complete = all(code in prices for code in account.codes())
        holdings = account.weights(prices) if complete else {}
        report = research_report or {}
        is_v2 = manager and report.get("strategy_version") == "v2"
        screen = (report.get("evidence") or {}).get("opportunities") if is_v2 and not replay else None
        if screen and (screen.get("application_date") != str(day) or screen.get("version") != 1):
            screen = None
        probe = None
        hard_stop = False
        research_targets = None
        notes = []
        if manager:
            if publication_source:
                problem = _unreviewed_reason(day, report, publication_source, station.now())
                if problem:
                    return {"status": "unreviewed", "summary": problem}
            proposed = report.get("targets")
            reason = report.get("rationale") or "本轮研究未给出交易目标，保持已确认持仓"
            drawdown = float(_last_snapshot(pod_id)["drawdown"] or 0)
            stop = abs(load_settings().agent.pod_hard_stop_drawdown)
            if drawdown <= -stop:
                hard_stop = True
                proposed = {}
                reason = f"已确认净值回撤 {drawdown:.2%} 触及 {stop:.0%} 硬止损线，覆盖研究目标，转为现金"
        elif complete:
            proposed, reason, _ = decide.targets_for(pod, market, universe, incumbent=holdings)
        else:
            proposed, reason = None, "持仓估值不完整，暂停调仓"

        targets, pending = {}, []
        timing = {}
        if not replay:
            timing = {"redeemable": otc.redeemable(account, day), "cash_receivable": otc.cash_receivable(pod_id, day)}
        if proposed is not None:
            try:
                targets, notes = execution.limit_targets(proposed)
                if is_v2:
                    research_targets = targets.copy()
                    prior = db.query_one(
                        f"SELECT payload FROM {db.qualified('pod_decision')} WHERE pod_id=%s AND trade_date=%s",
                        [pod_id, prev],
                    )
                    prior_card = prior["payload"] if prior else {}
                    previous = prior_card.get("research_targets") if prior_card.get("strategy_version") == "v2" else None
                    targets, delays = manager_v2.execution_targets(
                        account, prices, day, targets, previous,
                        report.get("evidence", {}).get("fund_features", {}), hard_stop=hard_stop,
                    )
                    if not replay and complete:
                        prior_probe = db.query_one(
                            f"SELECT payload->'opportunity' AS plan FROM {db.qualified('pod_decision')} "
                            "WHERE pod_id=%s AND trade_date<%s AND payload ? 'opportunity' "
                            "ORDER BY trade_date DESC LIMIT 1", [pod_id, day],
                        )
                        last = prior_probe['plan'] if prior_probe else None
                        if not screen and last:
                            screen = opportunity_research.missing_screen(day)
                            notes.append("本轮异动研究缺失，仅复核已有试仓的到期日及组合风控。")
                    if screen and complete:
                        turnover = opportunity_research.monthly_turnover(pod_id, day)
                        executable = opportunity.executable_core_targets(
                            targets, holdings, account.initial_capital, turnover["total"], turnover["probe_buys"]
                        )
                        if targets != executable:
                            notes.append("核心换手额度已耗尽，取消无法成交的核心加仓目标后按实际敞口复核试仓。")
                        targets, probe = opportunity_research.plan(
                            screen, account, holdings, executable, prices, day, last,
                            timing['cash_receivable'], report.get('stance') != 'cash' and not hard_stop,
                            hard_stop,
                        )
                        if probe.get("code"):
                            code = probe["code"]
                            schedule = screen["fee_schedules"].get(code)
                            if not schedule or schedule.get("buy") is None or not schedule.get("sell"):
                                schedule = (last or {}).get("fee_schedule")
                            if schedule:
                                probe["fee_schedule"] = schedule
                                account.fees[code] = schedule
                        notes.append("异动试仓：" + probe["reason"])
                    targets, limits = execution.limit_targets(targets)
                    notes.extend(delays + limits)
                if probe:
                    pending, checks = opportunity.prepare_orders(
                        account, targets, prices, monthly_turnover_left(pod, day), probe,
                        month_probe_buys=turnover['probe_buys'], total_month_turnover=turnover['total'],
                        isolated_reserve=True, **timing,
                    )
                    probe["instruction_submitted"] = any(
                        o["code"] == probe.get("code") and
                        ((probe["action"] == "enter" and o.get("opportunity_probe")) or
                         (probe["action"] == "exit" and o["side"] == "sell")) for o in pending)
                else:
                    pending, checks = execution.prepare_orders(
                        account, targets, prices, monthly_turnover_left(pod, day), **timing,
                    )
                if pending and not replay:
                    sessions = otc.sessions(day)
                    for order in pending:
                        order["otc"] = otc.clock(order["code"], sessions)
                        order["name"] = instruments[order["code"]]["name"]
                notes.extend(checks)
            except (TypeError, ValueError) as exc:
                notes.append(f"目标未通过风控校验，本轮维持持仓（{exc}）")
                proposed = None
        if not complete and proposed is None:
            notes.append("部分持仓缺少前一交易日净值，等待完整估值")
        if manager:
            summary = f"已冻结 {len(pending)} 笔模拟申赎指令" if pending else "本轮维持持仓"
            card = {
                "action": "pending" if pending else "hold", "summary": summary, "confidence": None,
                "model": report.get("model") or "system-rules", "prompt_version": "manager-v2" if is_v2 else "manager-v1",
                "rationale": "。".join([str(reason), *notes]), "long_term_view": pod["thesis"],
                "invalidation_note": "；".join(map(str, report.get("risks") or [])),
                "tomorrow_outlook": "", "newcomer_verdict": "", "payload": {},
            }
        else:
            prediction = decide.predict(pod, market, pod_store.nav_history(pod_id), holdings=holdings)
            snapshot = _last_snapshot(pod_id)
            card = decide.build_card(pod, market, targets, "。".join([reason, *notes]), [], snapshot,
                                     prediction, exposure=sum(targets.values()), pending=pending)
        card["payload"].update({
            **({"otc_clock_version": 1, "redeemable_shares": timing["redeemable"],
                "cash_receivable": timing["cash_receivable"],
                "spendable_cash": round(max(0, account.cash-timing["cash_receivable"]), 4)}
               if not replay else {}),
            **({"strategy_version": "v2", "research_targets": research_targets, "hard_stop": hard_stop} if is_v2 else {}),
            **({"opportunity": probe} if probe and proposed is not None else {}),
            "targets": targets if proposed is not None else None, "pending": pending,
            "instructions": pending, "research_id": report.get("id"), "risk_checks": notes,
            "price_as_of": str(prev) if prev else None, "pricing": "otc_nav",
            "settlement": {"status": "pending"}, "mode": "replay" if replay else "paper",
            "fee_rates": {order["code"]: account.fees.get(order["code"], {}) for order in pending},
        })
        if publication_source:
            from . import research
            reviewed = station.now()
            changed = research.decision_evidence_since(datetime.fromisoformat(report["as_of"]), reviewed,
                                                       exclude_report_id=report["id"])
            if changed:
                return {"status": "changed", "summary": "研究取数后有新增资料需重新复核：" + "、".join(changed),
                        "reviewed_through": reviewed}
            reason = ("按最新可见资料完成正式复核" if publication_source == "final_review"
                      else "已核对预案之后的资料，未发现新的决策依据；采用当日已完成的预案")
            card["payload"]["planned_action"] = _planned_action(account, pending)
            card["payload"]["publication"] = _publication(day, publication_source, reason, report,
                                                          reviewed, station.now(), pending)
        if not replay and _missed_cutoff(day):
            return {"action": "hold", "status": "cutoff", "summary": "指令计算完成时已过 15:00 截止"}
        decide.save_card(pod_id, day, card, executed=False)
        if not manager:
            decide.save_prediction(pod_id, day, prediction, horizon=1)
        return card | {"status": "planned", "pending": pending, "price_as_of": prev}


def execute_pod(pod: Mapping, trade_date: date, card: Mapping | None = None) -> dict:
    """Confirm only stored instructions using exact-date NAVs, atomically and in order."""
    pod_id = pod["pod_id"]
    with db.transaction():
        db.query_one(f"SELECT pod_id FROM {db.qualified('pod')} WHERE pod_id = %s FOR UPDATE", [pod_id])
        row = db.query_one(
            f"SELECT decision_id, payload, executed_at FROM {db.qualified('pod_decision')} "
            "WHERE pod_id = %s AND trade_date = %s FOR UPDATE", [pod_id, trade_date],
        )
        if not row:
            return {}
        if row["executed_at"]:
            return {"status": "already_settled", "fills": 0}
        payload = dict(row["payload"] or {})
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        if payload.get("mode") != "replay" and (trade_date > now.date() or (trade_date == now.date() and now.hour < 15)):
            return _defer_confirmation(row["decision_id"], "waiting_nav", "等待上海市场收盘后确认当日基金净值")
        orders = payload.get("pending")
        barrier = _decision_barrier(pod_id, trade_date)
        duplicate = db.query_one(
            f"SELECT 1 AS found FROM {db.qualified('pod_order')} WHERE pod_id = %s "
            "AND execute_on = %s LIMIT 1", [pod_id, trade_date],
        )
        if duplicate:
            barrier = "该日已有旧版成交但指令缺少确认标记，暂停重复结算"
        if not isinstance(orders, list):
            barrier = "缺少冻结指令，无法还原提交时的交易数量"
        if barrier:
            return _defer_confirmation(row["decision_id"], "blocked", barrier)
        account = pod_store.load_account(pod, as_of=trade_date)
        needed = set(account.codes()) | {o["code"] for o in orders}
        prices = execution.nav_on(trade_date, needed, visible_by=now if payload.get("mode") != "replay" else None)
        missing = sorted(needed - set(prices))
        if missing:
            return _defer_confirmation(row["decision_id"], "waiting_nav",
                                       f"等待 {trade_date} 基金净值（{'、'.join(missing)}）", missing)
        had_holdings = bool(account.codes())
        account.fees.update(payload.get("fee_rates") or {})
        reason = (f"模拟场外申赎，按 {trade_date} 公布净值入账，份额登记与可赎日见冻结指令"
                  if payload.get("otc_clock_version") else f"模拟场外申赎，按 {trade_date} 公布净值确认")
        try:
            timing = {}
            if payload.get("otc_clock_version") == 1:
                timing = {"redeemable": otc.redeemable(account, trade_date),
                          "cash_receivable": otc.cash_receivable(pod_id, trade_date)}
            fills = execution.fill_orders(account, trade_date, orders, prices, reason, **timing)
        except (KeyError, ValueError, TypeError) as exc:
            return _defer_confirmation(row["decision_id"], "blocked", str(exc))
        pod_store.save_fills(pod_id, trade_date, trade_date, fills, reason)
        snapshot = pod_store.save_snapshot(pod, trade_date, account, prices)
        sides = {f.side for f in fills}
        if not fills:
            action, summary = "hold", "维持持仓，已确认当日估值"
        else:
            action = "rebalance"
            if not account.codes():
                action = "close"
            elif not had_holdings:
                action = "open"
            elif len(sides) == 1:
                action = "add" if sides == {"buy"} else "reduce"
            summary = (f"已按当日净值入账 {len(fills)} 笔模拟申赎"
                       if payload.get("otc_clock_version") else f"已按当日净值确认 {len(fills)} 笔模拟申赎")
        changes = {
            "instructions": orders, "pending": [], "snapshot": snapshot,
            "fills": [{"code": f.code, "side": f.side, "amount": f.amount, "shares": f.shares,
                       "nav": f.nav, "fee": f.fee} for f in fills],
            "settlement": {"status": "settled", "nav_date": str(trade_date)},
        }
        db.execute(
            f"UPDATE {db.qualified('pod_decision')} SET executed_at = now(), action = %s, "
            "summary = %s, payload = payload || %s WHERE decision_id = %s",
            [action, summary, Jsonb(changes), row["decision_id"]],
        )
        return {"status": "settled", "snapshot": snapshot, "fills": len(fills), "price_as_of": trade_date}


def _defer_confirmation(decision_id: int, status: str, message: str, missing=None) -> dict:
    info = {"status": status, "reason": message, "missing_nav": missing or []}
    db.execute(
        f"UPDATE {db.qualified('pod_decision')} SET payload = COALESCE(payload, '{{}}'::jsonb) || %s "
        "WHERE decision_id = %s", [Jsonb({"settlement": info}), decision_id],
    )
    return info | {"fills": 0}


def decide_pod(pod: Mapping, market: decide.MarketView, universe: dict) -> dict:
    """回放用：14:30 出指令 + 晚间成交，一次跑完。"""
    card = plan_pod(pod, market, universe, replay=True)
    executed = execute_pod(pod, market.trade_date, card)
    return card | {"snapshot": executed.get("snapshot")}


# ---------------------------------------------------------------- 预测结算

def score_predictions() -> int:
    """把已经能算出结果的预测回填（命中/实际收益/Brier 分）。

    低置信度视为**弃权**：仍然记录 Brier（0.5 的预测本来就该拿 0.25 分），
    但不计入命中率——否则"永远猜 50.5% 上涨"这种废话会把命中率稀释成市场涨跌比例。
    """
    rows = db.query(
        f"""
        WITH px AS (
            SELECT pod_id, trade_date, unit_nav FROM {db.qualified('pod_nav')}
        )
        SELECT p.prediction_id, p.prob_up, p.confidence, a.unit_nav AS nav0, b.unit_nav AS nav1
        FROM {db.qualified('pod_prediction')} p
        JOIN px a ON a.pod_id = p.pod_id AND a.trade_date = p.trade_date
        JOIN LATERAL (
            SELECT unit_nav FROM px n
            WHERE n.pod_id = p.pod_id AND n.trade_date > p.trade_date
            ORDER BY n.trade_date LIMIT 1
        ) b ON TRUE
        WHERE p.scored_at IS NULL AND p.target = 'pod_nav' AND p.horizon_days = 1
        """
    )
    updates = []
    for row in rows:
        nav0, nav1, prob = float(row["nav0"]), float(row["nav1"]), float(row["prob_up"])
        realized = nav1 / nav0 - 1.0
        outcome = 1.0 if realized > 0 else 0.0
        hit = None if row["confidence"] == "low" else ((prob >= 0.5) == (realized > 0))
        brier = (prob - outcome) ** 2
        updates.append((round(realized, 6), hit, round(brier, 6), row["prediction_id"]))
    if updates:
        db.execute_many(
            f"UPDATE {db.qualified('pod_prediction')} "
            "SET realized_return = %s, hit = %s, brier = %s, scored_at = now() "
            "WHERE prediction_id = %s",
            updates,
        )
    return len(updates)


def hit_stats(pod_id: int) -> tuple[float | None, int]:
    """返回（原始命中率, 有效样本数）。样本少时不要拿它下结论。"""
    row = db.query_one(
        f"SELECT count(*) AS n, avg(CASE WHEN hit THEN 1.0 ELSE 0.0 END) AS rate "
        f"FROM {db.qualified('pod_prediction')} WHERE pod_id = %s AND hit IS NOT NULL",
        [pod_id],
    )
    if not row or not row["n"]:
        return None, 0
    return float(row["rate"]), int(row["n"])


def hit_rate(pod_id: int) -> float | None:
    return hit_stats(pod_id)[0]


def hit_rate_smoothed(pod_id: int, prior: float = 0.5, strength: int = 20) -> float:
    """向 0.5 收缩的命中率：1 次命中不等于 100% 的水平。

    没有这个收缩，刚开一天的仓会因为一条"猜对了"的预测拿到高综合分，
    直接把"今日最看好"发给用户——这正是最该避免的那种自信。
    """
    rate, n = hit_stats(pod_id)
    if rate is None:
        return prior
    return (rate * n + prior * strength) / (n + strength)


# ---------------------------------------------------------------- 考核

def evaluate_pods(market: decide.MarketView) -> list[dict]:
    """止损 → 到期淘汰。判定全部来自 pod_rules，不做主观干预。"""
    rules = pod_store.load_rules()
    agent = load_settings().agent
    actions: list[dict] = []

    for pod in pod_store.list_pods(status=["active", "observing"]):
        history = pod_store.nav_history(pod["pod_id"])
        if history.empty:
            continue
        latest = history.iloc[-1]
        drawdown = float(latest["drawdown"])
        cum_return = float(latest["cum_return"])
        benchmark = float(latest["benchmark_return"]) if pd.notna(latest["benchmark_return"]) else 0.0
        days = trade_days_between(pod["opened_on"], market.trade_date)

        if rules.hard_stop(drawdown):
            pod_store.set_status(
                pod["pod_id"], "suspended", market.trade_date,
                f"回撤 {drawdown:.2%} 触及硬止损线 {-rules.hard_stop_drawdown:.0%}，立即停牌",
                rule="hard_stop_drawdown",
            )
            actions.append({"pod": pod["pod_no"], "action": "suspend", "drawdown": drawdown})
            continue

        retire, reason = rules.should_retire(days, cum_return, benchmark, hit_rate_smoothed(pod["pod_id"]))
        if retire:
            pod_store.set_status(pod["pod_id"], "retired", market.trade_date, reason, rule="soft_retire")
            actions.append({"pod": pod["pod_no"], "action": "retire", "reason": reason})

    # 允许 agent 主动收缩：仓数超上限时，退役考核期内表现最差的那个
    active = pod_store.list_pods(status=["active"])
    if len(active) > agent.max_pods:
        worst = min(active, key=lambda item: _pod_metrics(item["pod_id"])["composite"])
        pod_store.set_status(
            worst["pod_id"], "retired", market.trade_date,
            f"存活仓数 {len(active)} 超过上限 {agent.max_pods}，退役综合表现最差的一仓",
            rule="max_pods",
        )
        actions.append({"pod": worst["pod_no"], "action": "retire", "reason": "exceeds max_pods"})

    return actions


def _pod_metrics(pod_id: int) -> dict:
    row = db.query_one(
        f"SELECT * FROM {db.qualified('pod_nav')} WHERE pod_id = %s ORDER BY trade_date DESC LIMIT 1",
        [pod_id],
    )
    if not row:
        return {"composite": -99.0, "excess": None, "hit_rate": None, "cum_return": None, "drawdown": None}
    excess = float(row["excess_return"]) if row["excess_return"] is not None else 0.0
    cum_return = float(row["cum_return"]) if row["cum_return"] is not None else 0.0
    drawdown = float(row["drawdown"]) if row["drawdown"] is not None else 0.0
    rate, samples = hit_stats(pod_id)
    composite = (
        100 * excess
        + 50 * cum_return
        + 20 * hit_rate_smoothed(pod_id)
        - 50 * abs(drawdown)
    )
    return {
        "composite": composite,
        "excess": excess,
        "hit_rate": rate,
        "hit_samples": samples,
        "cum_return": cum_return,
        "drawdown": drawdown,
    }


# ---------------------------------------------------------------- 开新仓

def should_open_pod(market: decide.MarketView, active: Sequence[Mapping]) -> tuple[bool, str, list[str]]:
    """返回 (是否开仓, 策略键, 建仓依据列表)。

    条件全部前置声明，避免"今天心情好就开一个仓"：
    * 未达仓数上限；
    * 与现存仓的板块重合度不超过阈值；
    * 同策略冷却期已过；
    * 有明确的行情机会（板块综合分超过阈值），或当前一个仓都没有（先跑基准仓）。
    """
    agent = load_settings().agent
    rules = pod_store.load_rules()

    if not agent.auto_open_pod:
        return False, "", ["自动开仓已关闭"]
    if len(active) >= agent.max_pods:
        return False, "", [f"已达仓数上限 {agent.max_pods}"]

    evidence: list[str] = []
    if market.has_boards:
        top = market.board_scores.head(1)
        board = top.index[0]
        score = float(top["score"].iloc[0])
        if score <= 1.0 and active:
            return False, "", [f"最强板块 {board} 综合分仅 {score:+.2f}，未达开仓阈值 1.0"]
        evidence.append(
            f"{board} 综合分 {score:+.2f}（5日动量 {float(top['momentum_5'].iloc[0]):+.1%}，"
            f"资金流强度 {float(top['flow_ratio'].iloc[0]):+.1%}）"
        )
        # 信号足够强才集中火力，否则分散——这是事前定好的规矩，不是临场感觉
        if score >= 2.0:
            strategy_key = "momentum_flow"
            evidence.append("信号强度 ≥ 2.0，采用集中版（前 2 板块）")
        else:
            strategy_key = "momentum_flow_broad"
            evidence.append("信号强度不足 2.0，采用分散版（前 3 板块）")
        candidate_boards = list(market.board_scores.head(3).index)
    else:
        evidence.append("无板块数据，先建一个基金级动量仓作为对照组")
        candidate_boards = []
        strategy_key = "fund_momentum"

    for pod in active:
        # 一个策略只允许一个仓：同策略+不同板块，本质上还是同一个赌注，只是账单分成两张
        if pod["strategy_key"] == strategy_key:
            return False, "", [f"已有仓 #{pod['pod_no']} 在使用同一策略（{strategy_key}），不重复开仓"]
        existing = pod_store.pod_boards(pod["pod_id"])
        if candidate_boards:
            # 最强板块不许重复押注：两个仓同时押同一个板块，本质是一个仓，只是看起来分散
            if candidate_boards[0] in existing:
                return False, "", [f"最强板块 {candidate_boards[0]} 已由仓 #{pod['pod_no']} 持有，不重复押注"]
            # 板块重合度（Jaccard）作为第二道判据
            if board_overlap(candidate_boards, existing) > agent.open_max_board_overlap:
                return False, "", [f"与仓 #{pod['pod_no']} 板块重合度过高，不重复开仓"]

    # 开仓节奏：新仓之间至少隔若干个交易日，避免"一天一个仓"式的手痒
    last_open = db.query_one(
        f"SELECT max(opened_on) AS d FROM {db.qualified('pod')} WHERE status IN ('active', 'retired')"
    )
    if last_open and last_open["d"]:
        elapsed = trade_days_between(last_open["d"], market.trade_date)
        if elapsed < agent.pod_min_interval_days:
            return False, "", [f"距上一个仓开仓仅 {elapsed} 个交易日（间隔需 {agent.pod_min_interval_days} 日）"]

    recent = db.query_one(
        f"""
        SELECT max(closed_on) AS last_closed FROM {db.qualified('pod')}
        WHERE status = 'retired' AND strategy_key = %s
        """,
        [strategy_key],
    )
    if recent and recent["last_closed"]:
        elapsed = trade_days_between(recent["last_closed"], market.trade_date)
        if elapsed < rules.cooldown_days:
            return False, "", [f"同策略刚退役 {elapsed} 个交易日，冷却期内（需 {rules.cooldown_days} 日）"]

    return True, strategy_key, evidence


def open_pod(market: decide.MarketView, strategy_key: str, evidence: Sequence[str], universe: dict) -> int:
    top_boards = []
    if market.has_boards and strategy_key != "defensive":
        top_boards = list(market.board_scores.head(2).index)
    labels = {
        "momentum_flow": "板块集中轮动",
        "momentum_flow_broad": "板块分散轮动",
        "fund_momentum": "基金双动量",
        "defensive": "短债防御基准",
    }
    name = labels.get(strategy_key, strategy_key)
    if strategy_key == "defensive":
        thesis = (
            "防御基准仓：满仓短债，用来回答一个必须回答的问题——"
            "进攻仓赚的那点超额，值不值得承担这些波动。"
            + (f"依据：{'；'.join(evidence)}" if evidence else "")
        )
    else:
        thesis = (
            f"看好 {('、'.join(top_boards)) if top_boards else '宽基指数'}："
            f"短中期动量与主力资金流同时走强，按板块强弱每 2 个交易日复核一次配置。"
            + (f"依据：{'；'.join(evidence)}" if evidence else "")
        )
    invalidation = [
        "综合分跌回 0 以下且资金流连续 3 日为负",
        "单仓回撤超过 12%",
        "所选板块与基金池映射失效",
    ]
    return pod_store.create_pod(
        name=name,
        strategy_key=strategy_key,
        thesis=thesis,
        invalidation_rules=invalidation,
        entry_evidence={"evidence": list(evidence), "boards": top_boards, "trade_date": str(market.trade_date)},
        trade_date=market.trade_date,
    )


# ---------------------------------------------------------------- 今日最看好

def update_daily_pick(trade_date: date) -> list[dict]:
    active = pod_store.list_pods(status=["active"])
    if not active:
        return []
    ranked = sorted(((_pod_metrics(pod["pod_id"]), pod) for pod in active), key=lambda item: -item[0]["composite"])

    db.execute(f"DELETE FROM {db.qualified('daily_pick')} WHERE trade_date = %s", [trade_date])
    picks = []
    for rank, (metrics, pod) in enumerate(ranked, start=1):
        excess = metrics["excess"] or 0.0
        samples = metrics.get("hit_samples") or 0
        if metrics["hit_rate"] is not None:
            reason = (
                f"超额 {excess:+.2%}，累计 {metrics['cum_return']:+.2%}，"
                f"回撤 {metrics['drawdown']:.2%}，预测命中率 "
                f"{metrics['hit_rate'] * 100:.0f}%（有效样本 {samples} 条）"
            )
        else:
            reason = f"超额 {excess:+.2%}，累计 {metrics['cum_return']:+.2%}（暂无预测样本）"
        suggested = min(max(0.30 + 0.8 * excess, 0.10), 0.80)
        picks.append(
            {
                "rank": rank,
                "pod_no": pod["pod_no"],
                "name": pod["name"],
                "score": round(metrics["composite"], 4),
                "reason": reason,
                "suggested_position_pct": round(suggested, 4) if rank == 1 else None,
            }
        )
        db.execute(
            f"INSERT INTO {db.qualified('daily_pick')} "
            "(trade_date, rank, pod_id, score, reason, suggested_position_pct) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                trade_date, rank, pod["pod_id"], round(metrics["composite"], 4), reason,
                round(suggested, 4) if rank == 1 else None,
            ],
        )
    return picks


# ---------------------------------------------------------------- 主流程

def run_daily(trade_date: date | None = None, open_new: bool = True, as_of: date | None = None) -> dict:
    assert_replay_database()
    universe = load_universe()
    trade_date = trade_date or latest_trade_date()
    if trade_date is None:
        raise RuntimeError("交易日历为空，先跑 scripts/ingest.py calendar")

    market = decide.build_market_view(trade_date, as_of=as_of or trade_date, board_end=prev_trade_date(trade_date))
    result: dict = {"trade_date": str(market.trade_date), "cards": [], "lifecycle": [], "picks": [], "scored": 0}

    # 1. 结算昨天的预测（用已实现的数据）
    result["scored"] = score_predictions()

    # 2. 逐个仓出决策卡
    active = pod_store.list_pods(status=["active"])
    for pod in active:
        card = decide_pod(pod, market, universe)
        result["cards"].append(
            {"pod_no": pod["pod_no"], "action": card["action"], "summary": card["summary"]}
        )

    # 3. 考核
    result["lifecycle"] = evaluate_pods(market)

    # 4. 先按行情机会开进攻仓，再用防御仓兜住"最低仓数"
    agent = load_settings().agent
    active = pod_store.list_pods(status=["active"])
    if open_new:
        ok, strategy_key, evidence = should_open_pod(market, active)
        if ok:
            pod_id = open_pod(market, strategy_key, evidence, universe)
            pod = pod_store.get_pod(pod_id)
            card = decide_pod(pod, market, universe)
            result["lifecycle"].append(
                {"pod": pod["pod_no"], "action": "open", "reason": "；".join(evidence)}
            )
            result["cards"].append(
                {"pod_no": pod["pod_no"], "action": card["action"], "summary": card["summary"]}
            )

    # 4b. 兜底：任何时候都必须有一个防御基准仓。
    # 它不是"凑数"的仓，而是对照组——没有它，"进攻仓赚的超额值不值这些波动"这个问题永远答不了。
    active = pod_store.list_pods(status=["active"])
    has_baseline = any(pod["strategy_key"] == "defensive" for pod in active)
    if not has_baseline and len(active) < agent.max_pods:
        pod_id = open_pod(
            market, "defensive",
            ["缺少防御基准仓，补一个作为所有进攻仓的对照组"],
            universe,
        )
        pod = pod_store.get_pod(pod_id)
        card = decide_pod(pod, market, universe)
        result["lifecycle"].append(
            {"pod": pod["pod_no"], "action": "open", "reason": "补足防御基准仓（对照组）"}
        )
        result["cards"].append(
            {"pod_no": pod["pod_no"], "action": card["action"], "summary": card["summary"]}
        )

    # 5. 评选今日最看好
    result["picks"] = update_daily_pick(market.trade_date)
    result["market_notes"] = market.notes
    log.info("每日决策完成: %s", result)
    return result


# ---------------------------------------------------------------- 盘中冻结 / 晚间确认

def _protective_hold(pod, day, reason, report, reviewed_through=None):
    """Publish a final zero-order instruction without waiting on external providers or model workers."""
    from . import station
    with db.transaction() as conn:
        conn.execute("SET LOCAL lock_timeout='2s'")
        conn.execute("SET LOCAL statement_timeout='5s'")
        current = db.query_one(f"SELECT status FROM {db.qualified('pod')} WHERE pod_id=%s FOR UPDATE", [pod["pod_id"]])
        existing = db.query_one(f"SELECT action,summary FROM {db.qualified('pod_decision')} WHERE pod_id=%s AND trade_date=%s",
                                [pod["pod_id"], day])
        if existing:
            return {"status": "existing", **existing}
        if not current or current["status"] not in ("active", "observing") or day < pod["opened_on"] or not is_trade_day(day):
            return {"status": "blocked", "summary": "当前账户或日期不满足交易条件"}
        barrier = _decision_barrier(pod["pod_id"], day)
        if barrier and barrier not in reason:
            reason += "；" + barrier
        held = bool(_last_snapshot(pod["pod_id"]).get("market_value"))
        published = station.now()
        publication = _publication(day, "protective_hold", reason, report or {}, reviewed_through, published)
        card = {"action": "hold", "summary": "维持现有持仓" if held else "保持空仓", "confidence": None,
                "rationale": reason + "；本日不申购、不赎回。", "tomorrow_outlook": "", "long_term_view": pod["thesis"],
                "invalidation_note": "", "newcomer_verdict": "", "model": "deadline-risk-control",
                "prompt_version": "manager-v2", "payload": {"publication": publication,
                    "planned_action": "hold" if held else "cash", "instructions": [], "pending": [], "targets": None,
                    "research_id": (report or {}).get("id"), "risk_checks": [reason], "mode": "paper",
                    "settlement": {"status": "pending"}, "pricing": "otc_nav", "otc_clock_version": 1}}
        if _missed_cutoff(day):
            return {"status": "cutoff", "summary": "已过当日申赎截止，不能补写指令"}
        decide.save_card(pod["pod_id"], day, card, executed=False)
        return {"status": "planned", "action": "hold", "summary": card["summary"], "reason": reason}


def run_intraday(trade_date: date | None = None, universe: dict | None = None, *, deadline=False) -> dict:
    from . import station

    now = station.now()
    day = trade_date or now.date()
    if _missed_cutoff(day) or not is_trade_day(day):
        return {"skipped": "当前时间不在当日申赎窗口", "trade_date": str(day)}
    if deadline and now < station.scheduled_time(day, "decision_retry"):
        return {"skipped": "尚未到达独立发布时点"}
    pod = station.ensure_manager(publication=True)
    existing = db.query_one(f"SELECT decision_id FROM {db.qualified('pod_decision')} "
                            "WHERE pod_id=%s AND trade_date=%s", [pod["pod_id"], day])
    if existing:
        return {"skipped": "当日指令已经冻结", **existing}
    # The deadline worker reads only durable state. It never waits for the research worker lock.
    if deadline:
        report = station.completed_decision_research(day, now)
        if report and report["decision_source"] == "prepared_review":
            refresh = station.final_refresh_inputs(day)
            if not refresh:
                return _protective_hold(pod, day, "正式复核尚未确认本轮行情和新闻刷新，预案不足以提交交易", report)
            report["decision_inputs"] = refresh
        if not report:
            return _protective_hold(pod, day, "截至独立发布时点尚无当日已完成的有效预案或正式复核", None)
    else:
        report = station.decision_research()
    for attempt in range(2):
        source = report.get("decision_source", "final_review")
        try:
            card = plan_pod(pod, day, universe or {}, research_report=report, publication_source=source)
        except psycopg.Error:
            if not deadline:
                raise
            log.exception("指令复核数据库操作超时或失败，单独尝试冻结维持指令")
            return _protective_hold(pod, day, "发布窗口内未能完成账本和决策依据核对", report)
        if card["status"] == "changed" and not deadline and attempt == 0 and station.now() < station.scheduled_time(day, "decision_retry"):
            report = station.decision_research()
            continue
        if deadline and card["status"] in {"changed", "blocked", "unreviewed"}:
            return _protective_hold(pod, day, card["summary"], report, card.get("reviewed_through"))
        return {"trade_date": str(day), "report_id": report["id"], "status": card["status"],
                "action": card.get("action"), "summary": card["summary"]}


def run_settle(trade_date: date | None = None) -> dict:
    """按日期重试经理所有未确认的指令；净值晚到时保留原申赎日。"""
    from . import research, station

    now = station.now()
    day = min(trade_date or now.date(), now.date())
    pod = station.ensure_manager()
    rows = db.query(f"SELECT trade_date FROM {db.qualified('pod_decision')} "
                    "WHERE pod_id=%s AND trade_date<=%s AND executed_at IS NULL ORDER BY trade_date",
                    [pod["pod_id"], day])
    settled = []
    valued = []
    gap = None
    for row in rows:
        mark = execution.mark_to_market(pod, row["trade_date"])
        valued.extend(mark["valued"])
        if mark.get("waiting_date"):
            gap = mark
        result = execute_pod(pod, row["trade_date"])
        settled.append({"date": str(row["trade_date"]), "status": result.get("status"),
                        "fills": result.get("fills", 0)})
        if result.get("status") not in ("already_settled", "settled"):
            break
    mark = execution.mark_to_market(pod, day)
    valued.extend(mark["valued"])
    counts = research.settle_forecast_counts(now)
    return {"settled": settled, "valued": valued, "valuation_gap": mark if mark.get("waiting_date") else gap,
            "forecasts_scored": counts["boards"], "fund_forecasts_scored": counts["funds"]}
