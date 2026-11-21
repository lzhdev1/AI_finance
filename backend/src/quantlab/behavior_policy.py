"""Link point-in-time market hypotheses to published forecasts and prospective purchases."""

import re
from collections import defaultdict

from . import behavior, db


def hypotheses_cited(text, rows, refs):
    """Statements attributing a motive must be conditional and linked to that board."""
    motives = r"拉高|派发|洗盘|诱多|吸筹|收割|出货|对倒|操纵|(?:主力|庄家|机构).{0,5}(?:意图|打算|刻意|蓄意|计划)"
    for clause in re.split(r"[。；;！？]", text):
        if not re.search(motives, clause):
            continue
        subjects = [r for r in rows if r.get("board") and r["board"] in clause]
        if not subjects and len(rows) == 1:
            subjects = rows
        if not subjects or not re.search(r"可能|假设|若|如果|疑似|风险|待验|待验证|待检验", clause):
            return False
        if any(f"H{r['board_id']}" not in refs for r in subjects):
            return False
        if re.search(r"操纵|对倒|蓄意|刻意", clause):
            return False
        if (
            re.search(r"拉高|派发|诱多|收割|出货", clause)
            and not re.search(r"若|如果|一旦", clause)
            and any((r.get("behavior") or {}).get("regime") != "distribution" for r in subjects)
        ):
            return False
    return True


def annotate(pool, closed, as_of, base):
    ids = [r["board_id"] for r in pool]
    bars = behavior.load_bars(ids, closed, as_of)
    sessions = (
        [
            str(r["trade_date"])
            for r in db.query(
                f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date>%s ORDER BY 1 LIMIT 5",
                [base],
            )
        ]
        if base
        else []
    )
    for row in pool:
        analysis = behavior.analyze(row, bars.get(row["board_id"], []), as_of)
        for step in analysis["path"]:
            ix = step["day"] - 1
            step["target_date"] = sessions[ix] if 0 <= ix < len(sessions) else None
        row["behavior"] = analysis
        row["evidence_ids"].append(f"H{row['board_id']}")


def observations(ids, as_of):
    rows = db.query(
        f"""SELECT f.board_id,f.proxy_code,f.horizon_days,o.regime,f.base_date,f.target_date,
                   f.issued_at,f.scored_at,f.realized_return,f.prob_up,o.base_prob_up,o.version
            FROM {db.qualified("agent_behavior_observation")} o
            JOIN {db.qualified("agent_board_forecast")} f USING(forecast_id)
            WHERE f.board_id=ANY(%s) AND f.scored_at<=%s AND f.target_date<=%s
              AND f.realized_return IS NOT NULL""",
        [ids, as_of, as_of.date()],
    )
    result = defaultdict(list)
    for row in rows:
        result[row["board_id"], row["version"]].append(row)
    return result


def apply_feedback(row, forecast, samples, as_of):
    scenario = row["behavior"]
    result = behavior.feedback(
        samples,
        board_id=row["board_id"],
        proxy_code=row["proxy_code"],
        horizon_days=forecast["horizon_days"],
        regime=scenario["regime"],
        as_of=as_of,
    )
    base = forecast["prob_up"]
    adjusted = round(max(0.02, min(0.98, base + result["probability_shift"])), 4)
    forecast.update(
        base_prob_up=base,
        prob_up=adjusted,
        behavior_adjustment=round(adjusted - base, 4),
        behavior_version=scenario["version"],
        behavior_regime=scenario["regime"],
        behavior_feedback=result,
        behavior_locked=False,
    )


def fund_entry_limit(targets, held, fund_estimates, features):
    """A board signal can only increase exposure if the fund's own NAV supports the trade."""
    if targets is None:
        return None, []
    result, notes = dict(targets), []
    five_day = {f["code"]: f for f in fund_estimates if f["horizon_days"] == 5}
    for code, weight in targets.items():
        current = held.get(code, 0.0)
        if weight <= current:
            continue
        forecast = five_day.get(code, {})
        up, ret = forecast.get("prob_up"), forecast.get("expected_return")
        fee = features.get(code, {}).get("round_trip_fee")
        if up is None or ret is None or fee is None:
            reason = "缺少该基金自身五日净值预测或模拟往返费率"
        elif up < 0.5 or ret <= fee:
            reason = f"基金自身五日上涨概率 {up:.2%}，预期收益 {ret:+.2%}，覆盖模拟申赎费用所需涨幅 {fee:.2%}"
        else:
            continue
        terms = features.get(code, {})
        if terms.get("redemption_rate") is not None:
            reason += (
                f"（最早 {terms['application_date']} 可申购，若 {terms['evaluation_date']} 赎回，"
                f"模拟持有 {terms['holding_days']} 个自然日，赎回费率 {terms['redemption_rate']:.2%}）"
            )
        notes.append(f"{code} 暂停新增配置，{reason}；目标由 {weight:.2%} 调整至 {current:.2%}。")
        if current > 0:
            result[code] = current
        else:
            result.pop(code)
    return result, notes


def limit_additions(watch, targets, held):
    """Reduce only a proposed increase. Apply a shared fund's tightest signal once."""
    result = None if targets is None else dict(targets)
    limits = {}
    for row in watch:
        if row["intent"] == "allocate":
            for fund in row["funds"]:
                code = fund["code"]
                limits[code] = min(limits.get(code, 1.0), row["behavior"]["risk_multiplier"])
    if result is not None:
        for code, requested in targets.items():
            weight = held.get(code, 0.0)
            if requested > weight:
                result[code] = round(weight + (requested - weight) * limits.get(code, 1.0), 6)
    for row in watch:
        codes = {f["code"] for f in row["funds"]} if row["intent"] == "allocate" else set()
        before = sum(targets.get(c, 0) for c in codes) if targets is not None else None
        after = sum(result.get(c, 0) for c in codes) if result is not None else None
        row["behavior"]["impact"] = {
            "risk_multiplier": row["behavior"]["risk_multiplier"],
            "before_weight": before,
            "after_weight": after,
            "note": (
                f"行为风险检查把相关基金的研究目标从 {before:.2%} 降为 {after:.2%}，仅缩减新增部分。"
                if before is not None and before - after > 0.000001
                else "仅观察，本轮没有新增配置。"
                if row["intent"] == "observe"
                else "本轮行为风险检查未改变相关基金的研究目标。"
            ),
        }
    return result
