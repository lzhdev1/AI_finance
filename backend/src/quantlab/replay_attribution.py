"""Read-only, fill-by-fill attribution and forecast diagnosis for one historical paper account."""

import json
from collections import defaultdict, deque
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import mean
from zoneinfo import ZoneInfo

from . import db, pod_store, replay_verification
from .config import CONFIG_DIR
from .costs import holding_rate
from .replay_universe import terms_on

ZERO = Decimal(0)
CENT = Decimal("0.01")


def _portion(total, shares, remaining):
    return total if shares == remaining else total * shares / remaining


def _gain(series, start, end):
    first, last = series.get(start), series.get(end)
    return last / first - 1 if first and last and first > 0 and last > 0 else None


def _timing_and_effects(lot, calendar, benchmark, board_series, fund_series):
    """Value all arithmetic components on the same invested capital and actual dates."""
    start, end = lot["entry_date"], lot["mark_date"]
    i, j = calendar.index(start), calendar.index(end)
    prior = calendar[i - 5] if i >= 5 else None
    after = calendar[j + 5] if lot["status"] == "closed" and j + 5 < len(calendar) else None
    mkt = _gain(benchmark, start, end)
    sector = _gain(board_series, start, end)
    fund = lot["exit_nav"] / lot["entry_nav"] - 1
    basis = lot["entry_nav"] * lot["shares"]
    market = basis * mkt if mkt is not None else None
    selection = basis * (sector - mkt) if sector is not None and mkt is not None else None
    tracking = basis * (fund - sector) if sector is not None else None
    explained = sum((v for v in (market, selection, tracking) if v is not None), ZERO)
    price_pnl = lot["shares"] * (lot["exit_nav"] - lot["entry_nav"])
    return {
        "holding_days": (end - start).days,
        "holding_sessions": j - i,
        "fund_return": fund,
        "board_return": sector,
        "benchmark_return": mkt,
        "market_pnl": market,
        "selection_pnl": selection,
        "tracking_pnl": tracking,
        "unattributed_pnl": ZERO if selection is not None else price_pnl - explained,
        "rounding_pnl": lot["gross_pnl"] - price_pnl,
        "prior_window_start": prior,
        "post_window_end": after,
        "entry_prior_5d_return": _gain(fund_series, prior, start),
        "exit_next_5d_return": _gain(fund_series, end, after) if after else None,
    }


def _reference_windows(sessions, horizon, market, board):
    market_returns, excess_returns = [], []
    for start, end in zip(sessions, sessions[horizon:]):
        sector, index = _gain(board, start, end), _gain(market, start, end)
        if sector is not None and index is not None:
            market_returns.append(index)
            excess_returns.append(sector - index)
    count = len(market_returns)
    return (count, mean(market_returns), mean(excess_returns)) if count else (0, None, None)


def _window_effects(lot, reference):
    count, mean_market, mean_excess = reference
    basis = lot["entry_nav"] * lot["shares"]
    typical_market = basis * mean_market if count and lot["market_pnl"] is not None else None
    typical_selection = basis * mean_excess if count and lot["selection_pnl"] is not None else None
    return {
        "window_count": count,
        "mean_sector_excess_return": mean_excess,
        "mean_market_return": mean_market,
        "typical_market_pnl": typical_market,
        "market_timing_pnl": lot["market_pnl"] - typical_market
        if typical_market is not None
        else None,
        "typical_selection_pnl": typical_selection,
        "selection_timing_pnl": lot["selection_pnl"] - typical_selection
        if typical_selection is not None
        else None,
    }


def _slice(buy, sell, qty, proceeds, fee, mark, mark_nav, allocation):
    amount = _portion(buy["cost_left"], qty, buy["left"])
    buy_fee = _portion(buy["fee_left"], qty, buy["left"])
    buy["cost_left"] -= amount
    buy["fee_left"] -= buy_fee
    buy["left"] -= qty
    i, code = buy["order_id"], buy["code"]
    sale_id = sell["order_id"] if sell else None
    gross = proceeds - (amount - buy_fee)
    return {
        "lot_id": f"{i}/{sale_id or 'open'}",
        "code": code,
        "name": buy["name"],
        "board": buy["board"],
        "board_id": buy["board_id"],
        "mapping_source": buy["mapping_source"],
        "mapping_basis": buy.get("mapping_basis"),
        "mapping_date": buy.get("mapping_date"),
        "buy_order_id": i,
        "sell_order_id": sale_id,
        "entry_date": buy["date"],
        "exit_date": sell["date"] if sell else None,
        "mark_date": mark,
        "status": "closed" if sell else "open",
        "shares": qty,
        "entry_nav": buy["nav"],
        "exit_nav": mark_nav,
        "cost_basis": amount,
        "proceeds": proceeds,
        "buy_fee": buy_fee,
        "sell_fee": fee,
        "fee_allocation": allocation,
        "gross_pnl": gross,
        "net_pnl": gross - buy_fee - fee,
    }


def fifo_lots(orders, fees, end, fund_prices):
    """Consume actual filled shares; reconcile each fill's posted amount and fee, including rounding."""
    holdings = defaultdict(deque)
    lots = []
    for order in orders:
        code = order["code"]
        if order["side"] == "buy":
            holdings[code].append(
                {
                    **order,
                    "left": order["shares"],
                    "fee_left": order["fee"],
                    "cost_left": order["amount"],
                }
            )
            continue
        if order["side"] != "sell":
            raise ValueError(f"未知成交方向 {order['side']}")
        qty_left = order["shares"]
        matches = []
        for buy in holdings[code]:
            qty = min(buy["left"], qty_left)
            days = (order["date"] - buy["date"]).days
            rate = Decimal(str(holding_rate(fees[code]["sell"], days)))
            matches.append((buy, qty, qty * order["nav"] * rate))
            qty_left -= qty
            if not qty_left:
                break
        if qty_left:
            raise ValueError(f"成交 {order['order_id']} 的赎回份额超过可核对批次")
        calculated_fee = sum((fee for _, _, fee in matches), ZERO)
        if abs(calculated_fee - order["fee"]) > Decimal("0.00011") and len(matches) > 1:
            raise ValueError(
                f"成交 {order['order_id']} 冻结费用与费率档位不符，无法确定各批次的实际费用"
            )
        shares_left, proceeds_left, fee_left = order["shares"], order["amount"], order["fee"]
        for buy, qty, theoretical in matches:
            proceeds = _portion(proceeds_left, qty, shares_left)
            fee = (
                fee_left
                if qty == shares_left
                else (
                    _portion(order["fee"], theoretical, calculated_fee) if calculated_fee else ZERO
                )
            )
            lots.append(
                _slice(
                    buy,
                    order,
                    qty,
                    proceeds,
                    fee,
                    order["date"],
                    order["nav"],
                    "成交费按FIFO持有天数及适用费率分配",
                )
            )
            shares_left -= qty
            proceeds_left -= proceeds
            fee_left -= fee
            if not buy["left"]:
                holdings[code].popleft()
    for code, queue in holdings.items():
        for buy in queue:
            nav = fund_prices[code].get(end)
            if not nav:
                raise ValueError(f"{code} 缺少期末确认净值 {end}")
            qty = buy["left"]
            lots.append(_slice(buy, None, qty, qty * nav, ZERO, end, nav, "持仓尚未赎回"))
    return lots


def _group(label, rows):
    scored = [r for r in rows if r["actual"] is not None]
    directional = [r for r in scored if r["hit"] is not None]
    hits = sum(r["hit"] for r in directional)
    return {
        "label": label,
        "total": len(rows),
        "scored": len(scored),
        "directional": len(directional),
        "hits": hits,
        "hit_rate": hits / len(directional) if directional else None,
        "brier": mean((r["prob_up"] - int(r["actual"] > 0)) ** 2 for r in scored)
        if scored
        else None,
        "mean_probability": mean(r["prob_up"] for r in scored) if scored else None,
        "up_rate": mean(r["actual"] > 0 for r in scored) if scored else None,
        "mean_samples": mean(r["samples"] for r in rows) if rows else None,
        "mean_return": mean(r["actual"] for r in scored) if scored else None,
    }


def _groups(rows, key, labels=None):
    partition = defaultdict(list)
    for row in rows:
        partition[key(row)].append(row)
    return [_group(label, partition[label]) for label in (labels or sorted(partition))]


def _bucket(value, thresholds):
    return sum(value >= t for t in thresholds)


def diagnose(rows, through, methods):
    diagnostics = []
    bins = ["低于40%", "40%–不足45%", "45%–不足50%", "50%–不足55%", "55%–不足60%", "60%及以上"]
    directions = ["看跌", "中性", "看涨"]
    sample_labels = ["少于15个", "15–29个", "30个及以上"]
    momentum_labels = ["前5日下跌", "前5日持平", "前5日上涨", "缺少动量"]
    strength = ["综合分低于20", "综合分20至不足80", "综合分80及以上", "缺少分数"]
    edge = ["偏离不足5个百分点", "偏离5至不足10个百分点", "偏离10个百分点及以上"]
    for horizon in (1, 5):
        group = [r for r in rows if r["horizon_days"] == horizon]
        data = _group(str(horizon), group)
        boards = sorted(
            _groups(group, lambda r: r["board"]), key=lambda r: (-r["total"], r["label"])
        )
        reversals = [
            r
            for r in group
            if r["actual"] is not None and r["actual"] != 0 and r["momentum"] not in (None, 0)
        ]
        baseline_rate = data["up_rate"]
        data.update(
            horizon_days=horizon,
            method="；".join(methods),
            post_verified=sum(r["supplement"] for r in group),
            pending=sum(r["actual"] is None for r in group),
            overdue=sum(r["actual"] is None and r["target_date"] <= through for r in group),
            baseline_brier=0.25 if data["scored"] else None,
            observed_base_rate_brier=baseline_rate * (1 - baseline_rate)
            if baseline_rate is not None
            else None,
            reversal_count=sum((r["actual"] > 0) != (r["momentum"] > 0) for r in reversals),
            reversal_sample=len(reversals),
            distinct_boards=len(boards),
            top_three_board_share=sum(r["total"] for r in boards[:3]) / len(group)
            if group
            else None,
            probability_bins=_groups(
                group, lambda r: bins[_bucket(r["prob_up"], [0.4, 0.45, 0.5, 0.55, 0.6])], bins
            ),
            by_direction=_groups(
                group,
                lambda r: directions[1 + int(r["prob_up"] > 0.5) - int(r["prob_up"] < 0.5)],
                directions,
            ),
            by_samples=_groups(
                group, lambda r: sample_labels[_bucket(r["samples"], [15, 30])], sample_labels
            ),
            by_momentum=_groups(
                group,
                lambda r: momentum_labels[
                    3
                    if r["momentum"] is None
                    else 1 + int(r["momentum"] > 0) - int(r["momentum"] < 0)
                ],
                momentum_labels,
            ),
            by_strength=_groups(
                group,
                lambda r: strength[3 if r["score"] is None else _bucket(r["score"], [20, 80])],
                strength,
            ),
            by_edge=_groups(
                group, lambda r: edge[_bucket(abs(round(r["prob_up"] - 0.5, 4)), [0.05, 0.1])], edge
            ),
            by_board=boards,
        )
        diagnostics.append(data)
    return diagnostics


def _forecast_rows(pod_id, start, as_of, complete, reports):
    t = db.qualified
    verify = (
        {r["forecast_id"]: r for r in replay_verification.list_verifications()} if complete else {}
    )
    cut = datetime.combine(as_of + timedelta(days=1), time(8, 30), ZoneInfo("Asia/Shanghai"))
    rows = db.query(
        f"""
        SELECT DISTINCT ON (f.forecast_id) f.forecast_id,f.prob_up,f.samples,f.horizon_days,f.target_date,
               f.board_id,f.board,f.realized_return,f.hit AS hit_original,f.scored_at,r.report_id
        FROM {t("agent_board_forecast")} f JOIN {t("agent_report_forecast")} r USING(forecast_id)
        JOIN {t("pod_decision")} d ON (d.payload->>'research_id')::bigint=r.report_id
        WHERE d.pod_id=%s AND d.trade_date BETWEEN %s AND %s AND d.executed_at IS NOT NULL
        ORDER BY f.forecast_id,d.trade_date""",
        [pod_id, start, as_of],
    )
    signals = {}
    methods = []
    for row in reports:
        payload = row["payload"]
        method = (payload.get("evidence") or {}).get("forecast_method")
        if method and method not in methods:
            methods.append(method)
        for watch in payload.get("watchlist", []):
            signals[row["report_id"], watch["board_id"]] = watch
    for r in rows:
        signal = signals.get((r["report_id"], r["board_id"]), {})
        r.update(
            momentum=signal.get("momentum_5"),
            score=signal.get("score"),
            actual=None,
            hit=None,
            supplement=False,
        )
        if r["scored_at"] is not None and r["scored_at"] <= cut and r["target_date"] <= as_of:
            r["actual"] = r["realized_return"]
            r["hit"] = r["hit_original"]
        elif r["target_date"] <= as_of and r["forecast_id"] in verify:
            evidence = verify[r["forecast_id"]]
            r.update(
                actual=float(evidence["realized_return"]), hit=evidence["hit"], supplement=True
            )
    diagnostics = diagnose(rows, as_of, methods)
    info_dates = [r["payload"]["data_as_of"] for r in reports if r["payload"].get("data_as_of")]
    is_v2 = any(r["payload"].get("strategy_version") == "v2" for r in reports)
    formula = (
        "原始上涨概率＝（5日与20日走势状态相似的上涨次数＋12×同板块无条件上涨概率）÷（相似样本数＋12）。"
        "无条件概率以12个中性先验样本收缩。用此前最多60个已到期日期的平均预测残差修正，每日等权，"
        "加30个零偏差先验，修正幅度限于正负10个百分点。"
        if is_v2
        else "上涨概率＝（五日动量方向相同的历史上涨次数＋5）÷（匹配样本数＋10）。"
        "至少需要8个匹配样本。1日与5日各自计算，5日训练收益按5日步长倒序抽取。"
    )
    for diagnostic in diagnostics:
        diagnostic["method"] += " " + formula
        diagnostic["first_input_date"] = min(info_dates) if info_dates else None
        diagnostic["last_input_date"] = max(info_dates) if info_dates else None
        diagnostic["score_through"] = as_of
    return diagnostics


def _map_orders(orders, by_day):
    previous_buys = {}
    for row in orders:
        day, code = row["date"], row["code"]
        report = by_day[day]
        terms = next((f for f in terms_on(day) if f["code"] == code), None)
        if terms:
            row["name"] = terms["name"]
        row.update(
            board_id=None,
            board=None,
            mapping_source=None,
            mapping_basis=None,
            mapping_date=None,
            lot_ids=[],
        )
        watch = report.get("watchlist", [])
        candidates = (report.get("evidence") or {}).get("candidates", [])
        for label, items in [
            ("当日冻结的配置板块", [w for w in watch if w.get("intent") == "allocate"]),
            ("当日冻结的跟踪板块", watch),
            ("当日冻结的研究候选", candidates),
        ]:
            matches = {
                w["board_id"]: (w, f)
                for w in items
                for f in w.get("funds", [])
                if f["code"] == code
            }
            if len(matches) == 1:
                w, f = next(iter(matches.values()))
                row.update(
                    board_id=w["board_id"],
                    board=w["board"],
                    name=f.get("name") or row["name"],
                    mapping_source=f.get("mapping_source") or "冻结研究报告",
                    mapping_basis=label,
                    mapping_date=day,
                )
                break
            if matches:
                # Competing contemporaneous themes cannot be resolved by taking the first name.
                prev = previous_buys.get(code)
                if prev and prev["board_id"] in matches:
                    row.update(**prev, mapping_basis="先前实际申购的冻结映射")
                break
        else:
            if code in previous_buys:
                row.update(**previous_buys[code], mapping_basis="先前实际申购的冻结映射")
        if row["side"] == "buy" and row["board_id"] is not None:
            previous_buys[code] = {
                k: row[k] for k in ("board_id", "board", "mapping_source", "mapping_date")
            }


def _json(value):
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _review_redemption_fees(orders, lots):
    sells = [o for o in orders if o["side"] == "sell"]
    codes = sorted({o["code"] for o in sells})
    stored = {
        r["code"]
        for r in db.query(
            f"""SELECT DISTINCT i.code FROM {db.qualified("instrument_fee")} f
            JOIN {db.qualified("instrument")} i USING(instrument_id)
            WHERE f.side='sell' AND i.code=ANY(%s)""",
            [codes],
        )
    }
    fallback = sum(o["code"] not in stored for o in sells)
    note = (
        f"本期 {len(sells)} 笔模拟赎回中，{fallback} 笔对应基金在专库费率表缺少赎回档位，"
        "缺档时按持有不足7个自然日收1.5%、满7日免收。报告损益以模拟成交费用为准。"
    )
    review = json.loads((CONFIG_DIR / "replay_redemption_fees.json").read_text(encoding="utf-8"))
    funds = {f["code"]: f for f in review["funds"]}
    evidence, expected = {}, defaultdict(Decimal)
    covered_lots = 0
    closed = [lot for lot in lots if lot["status"] == "closed"]
    for lot in closed:
        code, day = lot["code"], lot["exit_date"].isoformat()
        fund = funds.get(code)
        if not fund or not review["period"][0] <= day <= review["period"][1]:
            continue
        versions = [d for d in fund["documents"] if d["published"] <= day]
        if not versions:
            continue
        doc = max(versions, key=lambda d: d["published"])
        rate = next(
            rate for limit, rate in fund["rates"] if limit is None or lot["holding_days"] < limit
        )
        amount = lot["shares"] * lot["exit_nav"] * Decimal(str(rate))
        lot.update(
            disclosed_redemption_rate=rate,
            fee_document_url=doc["url"],
            disclosed_fee_before_rounding=amount,
        )
        expected[lot["sell_order_id"]] += amount
        covered_lots += 1
        evidence[doc["url"]] = {**doc, "code": code, "rates": fund["rates"]}
    differences = []
    booked, disclosed = ZERO, ZERO
    for order in sells:
        ident = order["order_id"]
        if ident in expected:
            calculated = expected[ident].quantize(Decimal("0.0001"))
            booked += order["fee"]
            disclosed += calculated
            if calculated != order["fee"]:
                differences.append(
                    {
                        "order_id": ident,
                        "code": order["code"],
                        "date": order["date"],
                        "booked_fee": order["fee"],
                        "disclosed_fee": calculated,
                        "difference": calculated - order["fee"],
                    }
                )
    missing = [o for o in sells if o["order_id"] not in expected]
    delta = disclosed - booked if expected else None
    audit = {
        "closed_lots": len(closed),
        "covered_lots": covered_lots,
        "uncovered_lots": len(closed) - covered_lots,
        "covered_orders": len(expected),
        "uncovered_orders": len(missing),
        "fallback_orders": fallback,
        "covered_booked_fees": booked,
        "covered_disclosed_fees": disclosed,
        "covered_difference": delta,
        "uncovered_booked_fees": sum((o["fee"] for o in missing), ZERO),
        "uncovered_codes": sorted({o["code"] for o in missing}),
        "changed_orders": differences,
    }
    scope = (
        "按个人投资者场外C类公开标准费率事后核查，按成交账本的FIFO持有日数、已确认份额和赎回净值复算，"
        f"覆盖 {covered_lots}/{len(closed)} 批、{len(expected)}/{len(sells)} 笔赎回。"
    )
    if expected:
        scope += (
            f"已覆盖份额按公告费率合计 {disclosed:,.4f} 元，与对应模拟扣费相差 {delta:+,.4f} 元。"
        )
    if missing:
        scope += "缺少对应费率证据的基金为" + "、".join(audit["uncovered_codes"]) + "。"
    if any(d["code"] == "008089" for d in evidence.values()):
        scope += "华夏房地产ETF联接C在满7天至不足30天收0.50%，满30天免收。"
    return [note, scope], audit, list(evidence.values())


def build_report():
    """Return a JSON-safe report as of the last consecutive confirmed session of this replay."""
    with db.transaction():
        database = replay_verification._database()
        t = db.qualified
        accounts = db.query(
            f"SELECT pod_id,strategy_key,initial_capital,entry_evidence,benchmark_code FROM {t('pod')}"
        )
        if len(accounts) != 1 or accounts[0]["strategy_key"] != "agent_manager":
            raise ValueError("归因需要唯一的独立经理账户")
        pod = accounts[0]
        manifest = pod["entry_evidence"]
        if manifest.get("mode") != "historical_replay" or manifest.get("run_id") != database:
            raise ValueError("归因账户不是本库独立回放")
        start, end = (date.fromisoformat(manifest[k]) for k in ("start", "end"))
        pod_id = pod["pod_id"]
        calendar = [
            r["trade_date"]
            for r in db.query(
                f"SELECT trade_date FROM {t('trade_calendar')} WHERE trade_date<=%s ORDER BY 1",
                [end],
            )
        ]
        sessions = [d for d in calendar if d >= start]
        statements = db.query(
            f"""
            SELECT n.trade_date,n.total_value,n.cash,n.market_value,d.payload->>'research_id' AS report_id
            FROM {t("pod_nav")} n JOIN {t("pod_decision")} d USING(pod_id,trade_date)
            WHERE n.pod_id=%s AND d.executed_at IS NOT NULL AND n.trade_date BETWEEN %s AND %s ORDER BY n.trade_date""",
            [pod_id, start, end],
        )
        if not statements or [s["trade_date"] for s in statements] != sessions[: len(statements)]:
            raise ValueError("归因要求已有连续的确认净值和决策")
        through = statements[-1]["trade_date"]
        calendar = [d for d in calendar if d <= through]
        complete = len(statements) == len(sessions)
        reports = db.query(
            f"SELECT report_id,payload FROM {t('agent_research')} WHERE report_id=ANY(%s)",
            [[int(s["report_id"]) for s in statements]],
        )
        report_by_id = {r["report_id"]: r["payload"] for r in reports}
        by_day = {s["trade_date"]: report_by_id[int(s["report_id"])] for s in statements}
        orders = db.query(
            f"""
            SELECT o.order_id,o.execute_on AS date,i.code,i.name,o.side,o.amount,o.shares,o.nav,o.fee
            FROM {t("pod_order")} o JOIN {t("instrument")} i USING(instrument_id)
            WHERE o.pod_id=%s AND o.execute_on BETWEEN %s AND %s ORDER BY o.execute_on,o.order_id""",
            [pod_id, start, through],
        )
        codes = sorted({r["code"] for r in orders})
        _map_orders(orders, by_day)
        first_index = max(0, calendar.index(sessions[0]) - 5)
        price_start = calendar[first_index]
        fund_prices, board_prices = defaultdict(dict), defaultdict(dict)
        for row in db.query(
            f"""SELECT i.code,n.trade_date,n.nav FROM {t("instrument_nav")} n JOIN {t("instrument")} i USING(instrument_id)
                        WHERE i.code=ANY(%s) AND n.trade_date BETWEEN %s AND %s""",
            [codes, price_start, through],
        ):
            fund_prices[row["code"]][row["trade_date"]] = row["nav"]
        for row in db.query(
            f"""SELECT board_id,trade_date,close FROM {t("board_quote")}
                        WHERE board_id=ANY(%s) AND trade_date BETWEEN %s AND %s""",
            [
                sorted({r["board_id"] for r in orders if r["board_id"] is not None}),
                price_start,
                through,
            ],
        ):
            board_prices[row["board_id"]][row["trade_date"]] = row["close"]
        benchmark = {
            r["trade_date"]: r["close"]
            for r in db.query(
                f"""SELECT trade_date,close FROM {t("index_quote")}
                       WHERE index_code=%s AND trade_date BETWEEN %s AND %s""",
                [pod["benchmark_code"], price_start, through],
            )
        }
        lots = fifo_lots(orders, pod_store.fee_book(codes), through, fund_prices)
        fills_by_id = {o["order_id"]: o for o in orders}
        windows = {}
        reference_sessions = [s["trade_date"] for s in statements]
        for lot in lots:
            lot.update(
                _timing_and_effects(
                    lot,
                    calendar,
                    benchmark,
                    board_prices[lot["board_id"]],
                    fund_prices[lot["code"]],
                )
            )
            key = (lot["board_id"], lot["holding_sessions"])
            if key not in windows:
                windows[key] = _reference_windows(
                    reference_sessions, key[1], benchmark, board_prices[key[0]]
                )
            lot.update(_window_effects(lot, windows[key]))
            for key in ("buy_order_id", "sell_order_id"):
                if lot[key] is not None:
                    fills_by_id[lot[key]]["lot_ids"].append(lot["lot_id"])
        for order in orders:
            exits = [lot for lot in lots if lot["sell_order_id"] == order["order_id"]]
            order["realized_gross_pnl"] = (
                sum((lot["gross_pnl"] for lot in exits), ZERO) if exits else None
            )
            order["realized_net_pnl"] = (
                sum((lot["net_pnl"] for lot in exits), ZERO) if exits else None
            )
        summary = {
            k: sum((l[k] for l in lots if l[k] is not None), ZERO)
            for k in (
                "gross_pnl",
                "net_pnl",
                "market_pnl",
                "selection_pnl",
                "tracking_pnl",
                "unattributed_pnl",
                "rounding_pnl",
                "typical_market_pnl",
                "market_timing_pnl",
                "typical_selection_pnl",
                "selection_timing_pnl",
            )
        }
        summary["window_covered_lots"] = sum(l["selection_timing_pnl"] is not None for l in lots)
        summary["window_total_lots"] = len(lots)
        summary["uncompared_market_pnl"] = sum(
            (
                l["market_pnl"]
                for l in lots
                if l["market_pnl"] is not None and l["typical_market_pnl"] is None
            ),
            ZERO,
        )
        initial = pod["initial_capital"]
        net = summary["net_pnl"]
        fees = sum((o["fee"] for o in orders), ZERO)
        realized = sum((l["net_pnl"] for l in lots if l["status"] == "closed"), ZERO)
        delta = statements[-1]["total_value"] - initial - net
        if abs(delta) > CENT or abs(summary["gross_pnl"] - fees - net) > Decimal("0.00001"):
            raise ValueError(f"批次归因与已确认净值或手续费不符，净值差 {delta}")
        anchor = next((d for d in reversed(calendar) if d < start), None)
        benchmark_return = _gain(benchmark, anchor, through)
        full = initial * benchmark_return if benchmark_return is not None else None
        summary.update(
            initial_capital=initial,
            ending_value=statements[-1]["total_value"],
            fees=fees,
            realized_net_pnl=realized,
            unrealized_net_pnl=net - realized,
            reconciliation_difference=delta,
            full_market_pnl=full,
            exposure_timing_pnl=summary["market_pnl"] - full
            if full is not None and all(l["market_pnl"] is not None for l in lots)
            else None,
        )
        closed = [lot for lot in lots if lot["status"] == "closed"]
        short_fee = [lot for lot in closed if lot["holding_days"] < 7 and lot["sell_fee"] > 0]
        summary.update(
            profitable_lots=sum(l["net_pnl"] > 0 for l in closed),
            losing_lots=sum(l["net_pnl"] < 0 for l in closed),
            total_profits=sum((l["net_pnl"] for l in closed if l["net_pnl"] > 0), ZERO),
            total_losses=sum((l["net_pnl"] for l in closed if l["net_pnl"] < 0), ZERO),
            short_redemption_orders=len({l["sell_order_id"] for l in short_fee}),
            short_redemption_lots=len(short_fee),
            short_redemption_fees=sum((l["sell_fee"] for l in short_fee), ZERO),
        )
        version = manifest.get("strategy_version", "v1")
        fee_notes, fee_audit, fee_documents = _review_redemption_fees(orders, lots)
        return _json(
            {
                "version": version,
                "start": start,
                "end": end,
                "as_of": through,
                "complete": complete,
                "summary": summary,
                "orders": orders,
                "lots": lots,
                "redemption_fee_documents": fee_documents,
                "redemption_fee_audit": fee_audit,
                "forecast_diagnostics": _forecast_rows(pod_id, start, through, complete, reports),
                "methodology": [
                    "确认日按实际成交金额、净值与份额还原。FIFO 批次按自然日计算费率，买入成本含申购费，赎回金额为扣费前金额。期末仍持有的批次按当日确切净值计价。",
                    *fee_notes,
                    "持有期市场贡献＝买入成交净值×份额×同期沪深300价格指数收益。板块选择和基金跟踪分别为同资本乘以板块减市场、基金减板块的收益差。板块优先取当日冻结的配置、跟踪或候选研究，其次取该基金先前实际申购的冻结映射。无法确定映射或缺少收盘端点的部分单列未归因。主题对照可能与基金合同指数不同。",
                    "毛损益按成交金额核对，实际份额和金额舍入与净值重算的差额单列，毛损益扣除买卖费用得到净损益。期末金额按分记账。",
                    "入场前五个交易日、离场后五个交易日均为基金净值原始涨跌，区间端点缺失或超过报告截止日时留空。现金暴露和持仓时段对照为持有期市场贡献减全额初始资金持有沪深300的盈亏。这些对照不估计改变指令后的可实现净利润。",
                    "交易时段的事后对照固定每批原板块、本金和持有交易日数。遍历回放起日至报告截止日相同长度的窗口，仅取板块与沪深300两端都有确切收盘价的窗口并等权。平均超额收益乘本金为该板块的典型贡献，实际区间超额减典型贡献为所选时段偏差；市场贡献用同一批窗口作相同拆分。包含实际窗口，多个窗口可重叠，仅描述本期历史。",
                    "预测以唯一预测编号计数，平盘按未上涨处理；中性预测计入 Brier、退出方向胜率分母。0.25 为恒报50%的基准。历史五日动量反转率排除动量或实际收益等于零。板块排名采用报告发布时的综合得分。重叠未来五日窗口及同日不同板块预测可能相关。",
                    "训练样本数取预测发布时的记录，方向与收益频率按已到期可计分记录计算。期后验证单列，只计入完整回放的事后成绩。",
                ],
            }
        )
