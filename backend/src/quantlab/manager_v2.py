"""Version-two manager policy, fixed before the comparative replay."""

import pandas as pd

from . import db
from .costs import buy_fee, holding_rate

VERSION = "v2"
METHOD = (
    "使用已完成且互不重叠的同板块收益。以5日和20日趋势方向联合分组，用12个基础样本收缩；"
    "分别估计上涨概率和平均收益。仅用此前已到期的本版本预测做偏差修正，同到期日等权，"
    "最近60个到期日、30日中性先验，概率调整不超过10个百分点。"
)
PRIOR = 12
CALIBRATION_DAYS = 60
CALIBRATION_PRIOR = 30
RISK_VOL = 0.006


def calibration(as_of):
    rows = db.query(
        f"""
        WITH days AS (
            SELECT f.horizon_days,f.target_date,
                   avg((f.realized_return > 0)::int-f.prob_up) AS error
            FROM {db.qualified("agent_board_forecast")} f
            WHERE f.scored_at <= %s AND f.target_date <= %s AND f.realized_return IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM {db.qualified("agent_report_forecast")} x
                JOIN {db.qualified("agent_research")} r USING(report_id)
                WHERE x.forecast_id=f.forecast_id AND r.as_of=f.issued_at
                    AND r.payload->>'strategy_version'='v2')
            GROUP BY f.horizon_days,f.target_date
        ), recent AS (
            SELECT *,row_number() OVER (PARTITION BY horizon_days ORDER BY target_date DESC) seq FROM days
        )
        SELECT horizon_days,count(*)::int days,sum(error)::float residual,
               max(target_date) latest FROM recent WHERE seq<=%s GROUP BY horizon_days
    """,
        [as_of, as_of.date(), CALIBRATION_DAYS],
    )
    return {
        r["horizon_days"]: {
            "days": r["days"],
            "latest": str(r["latest"]),
            "shift": max(-0.1, min(0.1, r["residual"] / (r["days"] + CALIBRATION_PRIOR))),
        }
        for r in rows
    }


def forecast(prices, horizon, calibration_table, *, lead_sessions=0):
    if len(prices) < 25:
        return None
    ret5, ret20 = prices / prices.shift(5) - 1, prices / prices.shift(20) - 1
    if pd.isna(ret5.iloc[-1]) or pd.isna(ret20.iloc[-1]):
        return None
    regime = (ret5.iloc[-1] > 0, ret20.iloc[-1] > 0)
    # An OTC prediction before the close can observe yesterday's NAV while
    # predicting from today's still-unknown NAV to the target day's close.
    returns = prices.shift(-horizon - lead_sessions) / prices.shift(-lead_sessions) - 1
    all_returns, similar = [], []
    for i in range(len(prices) - lead_sessions - horizon - 1, 19, -horizon):
        if pd.isna(returns.iloc[i]) or pd.isna(ret5.iloc[i]) or pd.isna(ret20.iloc[i]):
            continue
        realized = float(returns.iloc[i])
        all_returns.append(realized)
        if (ret5.iloc[i] > 0, ret20.iloc[i] > 0) == regime:
            similar.append(realized)
    n, m = len(all_returns), len(similar)
    if n < PRIOR:
        return None
    unconditional = (sum(v > 0 for v in all_returns) + PRIOR / 2) / (n + PRIOR)
    mean = sum(all_returns) / (n + PRIOR)
    raw = (sum(v > 0 for v in similar) + PRIOR * unconditional) / (m + PRIOR)
    expected = (sum(similar) + PRIOR * mean) / (m + PRIOR)
    calib = calibration_table.get(horizon, {"days": 0, "latest": None, "shift": 0.0})
    p = max(0.02, min(0.98, raw + calib["shift"]))
    return {
        "prob_up": round(p, 4),
        "expected_return": round(expected, 6),
        "samples": m,
        "all_samples": n,
        "raw_prob_up": round(raw, 6),
        "calibration": calib,
        "confidence": "mid" if m >= 30 and abs(p - 0.5) >= 0.07 else "low",
    }


def fund_inputs(pool, history, fees, day, horizon_end):
    inputs = {}
    for row in pool:
        daily = history[row["board_id"]].pct_change(fill_method=None).tail(20).dropna()
        volatility = float(daily.std()) if len(daily) >= 12 else None
        outcome = next((f for f in row["outlooks"] if f["horizon_days"] == 5), {})
        for f in row["funds"]:
            code = f["code"]
            schedule = fees.get(code, {})
            proceeds = (
                (1 - buy_fee(1.0, schedule.get("buy", 0.0)))
                * (1 - holding_rate(schedule.get("sell", []), (horizon_end - day).days))
                if horizon_end
                else 0.0
            )
            round_trip = 1 / proceeds - 1 if proceeds > 0 else None
            # Multiple mappings can overlap. Keep the strongest downside estimate for execution risk.
            evidence = {
                "daily_volatility": volatility,
                "expected_return": outcome.get("expected_return"),
                "prob_up": outcome.get("prob_up"),
                "round_trip_fee": round_trip,
                "negative_trend": row["momentum_5"] < 0 and row["momentum_20"] < 0,
            }
            if code not in inputs or (evidence["expected_return"] or 0) < (
                inputs[code]["expected_return"] or 0
            ):
                inputs[code] = evidence
    return inputs


def entry_budget(targets, features, held):
    if targets is None:
        return None, []
    result, notes = {}, []
    for code, weight in targets.items():
        f = features.get(code, {})
        exp, p, cost, vol = (
            f.get(k) for k in ("expected_return", "prob_up", "round_trip_fee", "daily_volatility")
        )
        current = held.get(code, 0.0)
        if weight > current and (
            exp is None or p is None or cost is None or vol is None or p < 0.5 or exp <= cost
        ):
            notes.append(f"{code} 五日上涨概率、费后收益或波动样本未达新增条件。")
            weight = current
        if vol and weight > 0:
            capped = min(weight, RISK_VOL / vol)
            if capped < weight:
                notes.append(
                    f"{code} 近20日波动率 {vol:.2%}，单基金日波动预算限制目标为 {capped:.2%}。"
                )
            weight = capped
        if weight > 0:
            result[code] = round(weight, 6)
    return result, notes


def execution_targets(account, prices, day, proposed, previous, features, *, hard_stop=False):
    """Respect FIFO costs, cap unconfirmed rotation and never spend the retained risk allocation twice."""
    if hard_stop:
        return {}, ["硬止损优先，忽略费率与确认期，立即冻结全部可赎回份额。"]
    if not all(prices.get(code, 0) > 0 for code in account.codes()):
        return proposed, []
    total = account.total_value(prices)
    if not total:
        return proposed, []
    held = account.weights(prices)
    adjusted = dict(proposed)
    notes = []
    for code, want in proposed.items():
        if want > 0 and held.get(code, 0) == 0 and not (previous or {}).get(code, 0) > 0:
            adjusted.pop(code, None)
            notes.append(f"{code} 新建仓信号等待第二个连续交易日确认。")
    for code, current in held.items():
        want = adjusted.get(code, 0.0)
        if want >= current:
            continue
        f = features.get(code, {})
        downside = max(0.0, -(f.get("expected_return") or 0.0))
        urgent = bool(f.get("negative_trend") and downside > 0)
        if not urgent and (
            previous is None or previous.get(code, 0.0) >= current - min(0.02, current * 0.1)
        ):
            adjusted[code] = current
            notes.append(f"{code} 尚未连续两日降低目标，本轮保留已确认份额。")
            continue
        sell_units = (current - want) * total / prices[code]
        permitted = 0.0
        schedule = account.fees.get(code, {}).get("sell", [])
        for lot in account.lots[code]:
            available = min(lot.shares, sell_units - permitted)
            if available <= 0:
                break
            age = (day - lot.buy_date).days
            saving = holding_rate(schedule, age) - holding_rate(schedule, age + 7)
            if saving > 0 and saving >= downside:
                notes.append(
                    f"{code} FIFO批次持有{age}天，未来七天可节约赎回费 {saving:.2%}，推迟这部分赎回。"
                )
                break
            permitted += available
        if permitted < sell_units - 1e-6:
            adjusted[code] = max(want, current - permitted * prices[code] / total)
    # A delayed exit consumes risk budget. Reduce the new/additional purchases pro rata.
    buys = {
        code: w - held.get(code, 0.0) for code, w in adjusted.items() if w > held.get(code, 0.0)
    }
    total_buy = sum(buys.values())
    excess = max(0.0, sum(adjusted.values()) - sum(proposed.values()))
    if total_buy and excess:
        scale = max(0.0, 1 - excess / total_buy)
        for code, inc in buys.items():
            adjusted[code] = held.get(code, 0.0) + inc * scale
        notes.append("待确认或待降费的旧仓占用预算，压缩本轮新增申购。")
    return {c: w for c, w in adjusted.items() if w > 1e-8}, notes
