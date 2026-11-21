"""Read-only, independent reconciliation of a frozen manager replay and its published archive."""

import argparse
import json
import math
import re
from collections import Counter, defaultdict, deque
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from quantlab import db
from quantlab.config import DATA_DIR, load_settings

SH = ZoneInfo("Asia/Shanghai")


class Audit:
    def __init__(self):
        self.checks = Counter()
        self.failed = 0
        self.examples = []

    def check(self, category, ok, location):
        self.checks[category] += 1
        if not ok:
            self.failed += 1
            if len(self.examples) < 50:
                self.examples.append(f"{category} · {location}")

    def near(self, category, expected, actual, location, tolerance=1e-6):
        ok = (expected is None and actual is None) or (
            expected is not None
            and actual is not None
            and math.isclose(float(expected), float(actual), rel_tol=1e-10, abs_tol=tolerance)
        )
        self.check(category, ok, f"{location} expected={expected} actual={actual}")


def forecast_returns(audit, forecasts, calendar, prices, last_clock):
    by_id = {}
    for f in forecasts:
        key = f["forecast_id"]
        by_id[key] = f
        label = f"{key}/{f['board']}/{f['horizon_days']}T"
        base, end = f["base_date"], f["target_date"]
        pos = calendar.index(base) if base in calendar else -1
        h = f["horizon_days"]
        audit.check(
            "prediction calendar",
            h in (1, 5)
            and 0 <= pos < len(calendar) - h
            and calendar[pos + h] == end
            and f["issued_at"].astimezone(SH).date() == base,
            label,
        )
        series = prices.get(f["proxy_code"] or f["board_id"], {})
        p0, p1 = series.get(base), series.get(end)
        available = (
            datetime.combine(end + timedelta(days=1), time(8, 30), SH)
            if f["proxy_code"]
            else datetime.combine(end, time(15), SH)
        )
        if f["scored_at"] is None:
            audit.check(
                "pending outcomes",
                f["hit"] is None
                and f["realized_return"] is None
                and (not p0 or not p1 or available > last_clock),
                label,
            )
            continue
        audit.check(
            "outcome cutoff", available <= f["scored_at"] <= last_clock and bool(p0 and p1), label
        )
        if p0 and p1:
            ret = p1 / p0 - 1
            audit.near("exact outcome", ret, f["realized_return"], label, 1e-9)
            prob = f["prob_up"]
            audit.check(
                "direction", f["hit"] == (None if prob == 0.5 else (prob > 0.5) == (ret > 0)), label
            )
    return by_id


def verify_supplements(audit, rows, original, prices, boards, sessions, settled_days):
    """Reconcile late price evidence without adding it to any historical memory or quote series."""
    supplements = {}
    for row in rows:
        fid = row["forecast_id"]
        f = original.get(fid)
        audit.check("post-run original forecast", f is not None and fid not in supplements, fid)
        if not f:
            continue
        base, target = f["base_date"], f["target_date"]
        bid = f["board_id"]
        audit.check(
            "post-run verification scope",
            not f["proxy_code"]
            and f["scored_at"] is None
            and f["hit"] is None
            and f["realized_return"] is None
            and settled_days == len(sessions)
            and base < target <= sessions[-1],
            fid,
        )
        board = boards.get(bid, {})
        record_url = urlparse(row["source_url"])
        audit.check(
            "post-run original source",
            board.get("source") == row["source"] == "ths"
            and board.get("name") == row["board_name"] == f["board"]
            and row["board_id"] == bid
            and row["board_code"] == board.get("code")
            and row["base_date"] == base
            and row["target_date"] == target,
            fid,
        )
        audit.check(
            "post-run source URL",
            record_url.scheme == "https"
            and record_url.hostname == "q.10jqka.com.cn"
            and record_url.path.rstrip("/").split("/")[-2:] == ["code", board.get("code")],
            fid,
        )
        observed_at = row["verified_at"]
        audit.check(
            "post-run evidence timestamp",
            observed_at is not None
            and observed_at.tzinfo is not None
            and observed_at >= datetime.combine(sessions[-1], time(15), SH),
            fid,
        )
        p0, p1 = row["base_close"], row["target_close"]
        anchors = [
            (prices.get(bid, {}).get(base), row["original_base_close"], p0),
            (prices.get(bid, {}).get(target), row["original_target_close"], p1),
        ]
        audit.check(
            "frozen price missing at one endpoint",
            sum(actual is not None for actual, _, _ in anchors) == 1,
            fid,
        )
        for original_price, recorded_original, new_price in anchors:
            audit.near("original archive price", original_price, recorded_original, fid, 1e-8)
            if original_price is not None:
                audit.near("same index series price anchor", original_price, new_price, fid, 1e-8)
        if (
            p0 is None
            or p1 is None
            or not all(math.isfinite(float(v)) and float(v) > 0 for v in (p0, p1))
        ):
            audit.check("post-run valid prices", False, fid)
            continue
        ret = float(p1) / float(p0) - 1
        prob = f["prob_up"]
        hit = None if prob == 0.5 else (ret > 0) == (prob > 0.5)
        audit.near("post-run independently scored", ret, row["realized_return"], fid, 1e-9)
        audit.check("post-run independently checked direction", hit == row["hit"], fid)
        supplements[fid] = {
            "realized_return": ret,
            "hit": hit,
            "base_close": p0,
            "target_close": p1,
            "verification_url": row["source_url"],
            "verification_source": row["source"],
            "verified_at": observed_at.isoformat() if observed_at else None,
        }
    return supplements


def recalibrate_forecast(dates, prices, horizon):
    """Recompute the issue-time estimate using only prices already available before the cutoff."""
    if len(dates) < 6 or dates[-1] not in prices or dates[-6] not in prices:
        return None
    rising = prices[dates[-1]] > prices[dates[-6]]
    outcomes = []
    for origin in range(len(dates) - 1 - horizon, 4, -horizon):
        previous, current, matured = (
            prices.get(dates[i]) for i in (origin - 5, origin, origin + horizon)
        )
        if previous and current and matured and ((current > previous) == rising):
            outcomes.append(matured / current - 1)
    if len(outcomes) < 8:
        return None
    return (
        len(outcomes),
        round((sum(r > 0 for r in outcomes) + 5) / (len(outcomes) + 10), 4),
        round(sum(outcomes) / (len(outcomes) + 10), 6),
    )


def visible_calibration(forecasts, horizon, cutoff):
    """Rebuild shrinkage using only outcomes scored by the decision clock, one vote per date."""
    residuals = defaultdict(list)
    for f in forecasts:
        if (
            f["horizon_days"] == horizon
            and f["scored_at"]
            and f["scored_at"] <= cutoff
            and f["target_date"] <= cutoff.date()
            and f["realized_return"] is not None
        ):
            residuals[f["target_date"]].append(int(f["realized_return"] > 0) - f["prob_up"])
    dates = sorted(residuals)[-60:]
    daily = (math.fsum(residuals[d]) / len(residuals[d]) for d in dates)
    correction = math.fsum(daily) / (30 + len(dates))
    return {
        "days": len(dates),
        "latest": str(dates[-1]) if dates else None,
        "shift": max(-0.10, min(0.10, correction)),
    }


def verified_calibration(audit, rebuilt, frozen, location):
    audit.check(
        "calibration maturity count",
        frozen.get("days") == rebuilt["days"] and frozen.get("latest") == rebuilt["latest"],
        location,
    )
    # PostgreSQL aggregates doubles in plan-dependent order. A few ulps can change
    # a published four-decimal probability exactly at a half tick. Verify the
    # full-precision shift independently before using its original bits for rounding.
    shift = frozen.get("shift")
    agrees = isinstance(shift, (int, float)) and math.isclose(
        shift, rebuilt["shift"], rel_tol=0.0, abs_tol=8 * math.ulp(1.0)
    )
    audit.check(
        "calibration residual", agrees, f"{location} expected={rebuilt['shift']} actual={shift}"
    )
    return {**rebuilt, "shift": shift} if agrees else rebuilt


def reconstruct_v2(dates, prices, horizon, calibration):
    """Independent reconstruction from dated closes; a return's end must be known."""
    if len(dates) < 25 or any(not prices.get(dates[i]) for i in (-1, -6, -21)):
        return None
    trend = (prices[dates[-1]] > prices[dates[-6]], prices[dates[-1]] > prices[dates[-21]])
    population, matches = [], []
    for finish in range(len(dates) - 1, 19 + horizon, -horizon):
        origin = finish - horizon
        recent, older, start, end = (
            prices.get(dates[i]) for i in (origin - 5, origin - 20, origin, finish)
        )
        if not all((recent, older, start, end)):
            continue
        value = end / start - 1
        population.append(value)
        if (start > recent, start > older) == trend:
            matches.append(value)
    if len(population) < 12:
        return None
    baseline_prob = (6 + sum(x > 0 for x in population)) / (12 + len(population))
    baseline_return = sum(population) / (12 + len(population))
    raw = (12 * baseline_prob + sum(x > 0 for x in matches)) / (12 + len(matches))
    expected = (12 * baseline_return + sum(matches)) / (12 + len(matches))
    p = min(0.98, max(0.02, raw + calibration["shift"]))
    return {
        "samples": len(matches),
        "all_samples": len(population),
        "raw_prob_up": round(raw, 6),
        "prob_up": round(p, 4),
        "expected_return": round(expected, 6),
        "confidence": "mid" if len(matches) >= 30 and abs(p - 0.5) >= 0.07 else "low",
    }


def audit_report(audit, r, days, prices, news, forecasts):
    payload, cutoff = r["payload"], r["as_of"].astimezone(SH)
    day = cutoff.date()
    audit.check(
        "decision time",
        cutoff.time() == time(14, 30)
        and payload["run_mode"] == "historical_replay"
        and datetime.fromisoformat(payload["as_of"]) == cutoff,
        day,
    )
    prior = days[days.index(day) - 1]
    evidence = payload["evidence"]
    v2 = payload.get("strategy_version", "v1") == "v2"
    if v2:
        calibration = {h: visible_calibration(forecasts, h, cutoff) for h in (1, 5)}
        for horizon, values in calibration.items():
            saved = evidence["calibration"].get(
                str(horizon), {"days": 0, "latest": None, "shift": 0}
            )
            calibration[horizon] = verified_calibration(audit, values, saved, f"{day} {horizon}T")
    for value in (r["data_as_of"], evidence.get("nav_date")):
        if value:
            audit.check("data cutoff", str(value) <= str(prior), day)
    items = {x["id"]: x for x in evidence["news"]}
    refs = set()
    for c in [*evidence["candidates"], *payload["watchlist"]]:
        bid = c["board_id"]
        refs.add(f"B{bid}")
        asof = date.fromisoformat(c["as_of"])
        audit.check("candidate date", asof <= prior and asof in days, f"{day} {c['board']}")
        if asof not in days:
            continue
        pos = days.index(asof)
        series = prices.get(c["proxy_code"] or bid, {})
        for horizon in (5, 20):
            past = series.get(days[pos - horizon]) if pos >= horizon else None
            audit.near(
                "historical momentum",
                series[asof] / past - 1 if series.get(asof) and past else None,
                c[f"momentum_{horizon}"],
                f"{day} {c['board']} {horizon}",
            )
        for n in c["news"]:
            items[n["id"]] = n
        intra = c.get("intraday")
        if intra:
            refs.add(f"I{bid}")
            audit.check(
                "intraday cutoff",
                cutoff - timedelta(minutes=30) <= datetime.fromisoformat(intra["at"]) <= cutoff,
                day,
            )
        if c.get("outlooks"):
            refs.add(f"F{bid}")
            history = [d for d in days if day - timedelta(days=400) <= d < day][-220:]
            for forecast in c["outlooks"]:
                h = forecast["horizon_days"]
                key = f"{day} {c['board']} {h}T"
                known = [d for d in history if d <= asof]
                if v2:
                    rebuilt = reconstruct_v2(known, series, h, calibration[h])
                    audit.check("visible training samples", rebuilt is not None, key)
                    if rebuilt is not None:
                        for field in (
                            "samples",
                            "all_samples",
                            "raw_prob_up",
                            "prob_up",
                            "expected_return",
                        ):
                            audit.near(
                                "v2 visible " + field,
                                rebuilt[field],
                                forecast.get(field),
                                key,
                                1e-9,
                            )
                        audit.check(
                            "v2 visible confidence",
                            rebuilt["confidence"] == forecast.get("confidence"),
                            key,
                        )
                    cal = forecast.get("calibration", {})
                    audit.check(
                        "issued calibration cutoff",
                        cal.get("days") == calibration[h]["days"]
                        and cal.get("latest") == calibration[h]["latest"],
                        key,
                    )
                    audit.check(
                        "issued calibration bias",
                        cal.get("shift") == calibration[h]["shift"],
                        key,
                    )
                    continue
                estimate = recalibrate_forecast(known, series, h)
                audit.check(
                    "visible training samples",
                    estimate is not None and estimate[0] == forecast["samples"],
                    key,
                )
                if estimate:
                    audit.near("visible probability", estimate[1], forecast["prob_up"], key, 1e-9)
                    audit.near(
                        "visible expected return",
                        estimate[2],
                        forecast["expected_return"],
                        key,
                        1e-9,
                    )
    for nid, n in items.items():
        src = news.get(nid)
        refs.add(f"N{nid}")
        if not src:
            audit.check("news source", False, f"{day} N{nid}")
            continue
        published = src["published_at"]
        available = published + (
            timedelta(minutes=1) if src["source"] == "chinanews-finance" else timedelta()
        )
        audit.check(
            "news cutoff",
            cutoff - timedelta(days=3) <= published and available <= cutoff,
            f"{day} N{nid}",
        )
        audit.check(
            "news source",
            src["title"] == n["title"]
            and src["url"] == n["url"]
            and src["source"] == n["source"]
            and datetime.fromisoformat(n["published_at"]) == published,
            f"{day} N{nid}",
        )
    for m in [*evidence["considered_memories"], *payload["memories"]]:
        key = (m["board_id"], m["proxy_code"], m["horizon_days"])
        ref = f"M{key[0]}-{key[2]}"
        refs.add(ref)
        matured = [
            f
            for f in forecasts
            if (f["board_id"], f["proxy_code"], f["horizon_days"]) == key
            and f["scored_at"] is not None
            and f["scored_at"] <= cutoff
            and day - timedelta(days=180) <= f["target_date"] <= day
        ]
        directional = [f for f in matured if f["hit"] is not None]
        audit.check(
            "mature memory", len(directional) == m["samples"] and bool(directional), f"{day} {ref}"
        )
        if directional:
            audit.near(
                "memory rate",
                sum(x["hit"] for x in directional) / len(directional),
                m["hit_rate"],
                f"{day} {ref}",
            )
        if matured:
            audit.near(
                "memory mean",
                sum(x["realized_return"] for x in matured) / len(matured),
                m["mean_return"],
                f"{day} {ref}",
            )
            brier = sum((x["prob_up"] - (x["realized_return"] > 0)) ** 2 for x in matured) / len(
                matured
            )
            audit.near("memory brier", brier, m["brier"], f"{day} {ref}")
    cited = set(payload.get("summary_refs", [])).union(
        *(s.get("refs", []) for s in payload["watchlist"])
    )
    audit.check("source references", cited <= refs, f"{day} unresolved={sorted(cited - refs)}")
    for order in evidence["recent_orders"]:
        audit.check("known orders", order["execute_on"] <= str(prior), day)
    for holding in evidence["holdings"]:
        audit.check("known holdings", holding["trade_date"] <= str(prior), day)
    for old in evidence["prior_research"]:
        audit.check("prior report", datetime.fromisoformat(old["as_of"]) < cutoff, day)
    return cited


def audit_ledger(audit, navs, orders, positions, cards, prices, benchmark, anchor, capital):
    cash, shares = capital, defaultdict(float)
    lots = defaultdict(deque)
    previous_signal = None
    peak = 1.0
    bench_peak = 1.0
    worst = bench_worst = 0.0
    series = {}
    prev_unit = 1.0
    prev_drawdown = 0.0
    stop = abs(load_settings().agent.pod_hard_stop_drawdown)
    for n in navs:
        day = n["trade_date"]
        card = cards[day]
        instructions = {(x["code"], x["side"]): x for x in card["payload"]["instructions"]}
        audit.check("frozen fills", len(instructions) == len(orders[day]), day)
        hard_stop = "硬止损" in card["rationale"]
        audit.check("stop signal", hard_stop == (prev_drawdown <= -stop), day)
        if hard_stop:
            audit.check(
                "hard stop orders",
                not card["payload"]["targets"] and not any(o["side"] == "buy" for o in orders[day]),
                day,
            )
        audit.check(
            "no same-day cash recycling",
            sum(float(o["amount"]) for o in orders[day] if o["side"] == "buy") <= cash + 0.011,
            day,
        )
        v2 = card["payload"].get("strategy_version") == "v2"
        if v2:
            audit.check(
                "explicit stop priority", card["payload"].get("hard_stop") is hard_stop, day
            )
        for o in orders[day]:
            code, side = o["code"], o["side"]
            amount, units, fee = map(float, (o["amount"], o["shares"], o["fee"]))
            label = f"{day} {code} {side}"
            px = prices[code].get(day)
            audit.check(
                "trade dates",
                day == o["decided_on"] == o["execute_on"]
                and o["created_at"] == card["executed_at"],
                label,
            )
            audit.near("fill nav", px, o["nav"], label, 1e-8)
            ins = instructions.get((code, side))
            audit.check("frozen instruction", bool(ins), label)
            if ins:
                if side == "buy":
                    audit.near("buy amount", ins["amount"], amount, label, 0.011)
                else:
                    audit.near("sell units", ins["shares"], units, label, 0.000002)
            if px:
                audit.near(
                    "fill valuation",
                    amount - fee if side == "buy" else amount,
                    units * px,
                    label,
                    0.012,
                )
            if side == "buy":
                rates = card["payload"]["fee_rates"][code]
                rate = float(rates["buy"])
                audit.near(
                    "subscription fee", round(amount - amount / (1 + rate), 4), fee, label, 0.000051
                )
                if v2 and shares[code] <= 1e-6:
                    audit.check(
                        "two-session entry",
                        bool((previous_signal or {}).get(code))
                        and bool((card["payload"]["research_targets"] or {}).get(code)),
                        label,
                    )
                lots[code].append((day, units))
                cash -= amount
                shares[code] += units
            else:
                remaining = units
                schedule = card["payload"]["fee_rates"][code]["sell"]
                reconstructed = 0.0
                while remaining > 1e-8 and lots[code]:
                    acquired, owned = lots[code].popleft()
                    take = min(remaining, owned)
                    age = (day - acquired).days
                    rate = next(
                        (rate for limit, rate in schedule if age < limit),
                        schedule[-1][1] if schedule else 0.015,
                    )
                    reconstructed += take * float(o["nav"]) * rate
                    remaining -= take
                    if owned - take > 1e-8:
                        lots[code].appendleft((acquired, owned - take))
                audit.near("FIFO covered shares", 0, remaining, label, 0.000002)
                audit.near("FIFO redemption fee", round(reconstructed, 4), fee, label, 0.000051)
                cash += amount - fee
                shares[code] -= units
                audit.check("owned units", shares[code] >= -0.000002, label)
        audit.check("cash available", cash >= -0.02, day)
        day_positions = positions[day]
        positive = {c: q for c, q in shares.items() if q > 2e-6}
        audit.check("position set", set(positive) == set(day_positions), day)
        value = 0.0
        for code, qty in positive.items():
            px = prices[code].get(day)
            audit.check("held price", px is not None, f"{day} {code}")
            if px is not None:
                value += qty * px
            if code in day_positions:
                audit.near(
                    "position units", qty, day_positions[code]["shares"], f"{day} {code}", 0.000003
                )
                if px:
                    audit.near(
                        "position value",
                        qty * px,
                        day_positions[code]["market_value"],
                        f"{day} {code}",
                        0.021,
                    )
        total = cash + value
        unit = total / capital
        peak = max(peak, unit)
        drawdown = unit / peak - 1
        worst = min(worst, drawdown)
        bench = benchmark[day] / benchmark[anchor]
        bench_peak = max(bench_peak, bench)
        bench_worst = min(bench_worst, bench / bench_peak - 1)
        for key, expected, eps in (
            ("cash", cash, 0.04),
            ("market_value", value, 0.03),
            ("total_value", total, 0.05),
            ("unit_nav", unit, 1.6e-6),
            ("cum_return", unit - 1, 1.6e-6),
            ("daily_return", unit / prev_unit - 1, 2e-6),
            ("drawdown", drawdown, 2e-6),
            ("benchmark_return", bench - 1, 1e-6),
            ("excess_return", unit - bench, 2e-6),
        ):
            audit.near("daily " + key, expected, n[key], day, eps)
        series[day] = {
            "cash": cash,
            "total_value": total,
            "unit_nav": unit,
            "cum_return": unit - 1,
            "max_drawdown": worst,
            "benchmark_return": bench - 1,
            "benchmark_max_drawdown": bench_worst,
            "excess_return": unit - bench,
        }
        prev_unit = unit
        prev_drawdown = float(n["drawdown"])
        previous_signal = card["payload"].get("research_targets")
    return series


def audit_archive(
    audit,
    archive,
    pod,
    sessions,
    series,
    cards,
    orders,
    refs,
    forecasts,
    linked,
    anchor,
    supplements,
):
    summary = archive["summary"]
    visible = sorted(archive["days"], key=lambda x: x["trade_date"])
    completed = len(visible)
    audit.check("archive identity", summary["id"] == pod["entry_evidence"]["run_id"], "run")
    audit.check(
        "archive period",
        summary["period"]["completed"] == completed
        and summary["period"]["sessions"] == len(sessions)
        and summary["period"]["start"] == str(sessions[0])
        and summary["period"]["end"] == str(sessions[-1]),
        "period",
    )
    audit.check(
        "archive sessions",
        [d["trade_date"] for d in visible] == [str(d) for d in sessions[:completed]],
        "daily records",
    )
    audit.check(
        "archive state", (summary["status"] == "ready") == (completed == len(sessions)), "status"
    )
    if not completed:
        return
    last = date.fromisoformat(visible[-1]["trade_date"])
    audit.check("archive DB prefix", last in series, last)
    if last not in series:
        return
    stamp = datetime.combine(last + timedelta(days=1), time(8, 30), SH)
    # Per-day source audits still read the unmodified ledger. Only a complete archive
    # may use the independently verified supplemental observations in its published results.
    post = supplements if completed == len(sessions) and summary["status"] == "ready" else {}
    outcomes = {
        fid: (
            post[fid]
            if fid in post
            else f
            if f["scored_at"] is not None and f["scored_at"] <= stamp
            else None
        )
        for fid, f in forecasts.items()
    }
    curves = summary["history"]
    audit.check("curve length", len(curves) == len(visible), last)
    exported_ids = set()
    for d, point in zip(visible, curves):
        day = date.fromisoformat(d["trade_date"])
        expected = series[day]
        card = cards[day]
        audit.check("daily action", d["action"] == card["action"], day)
        audit.check("curve date", point["trade_date"] == str(day), day)
        for k in ("cash", "total_value", "unit_nav", "cum_return"):
            audit.near(
                "archive daily " + k,
                expected[k],
                d[k],
                day,
                0.05 if k in ("cash", "total_value") else 2e-6,
            )
        for k in ("unit_nav", "cum_return", "benchmark_return"):
            audit.near("archive curve " + k, expected[k], point[k], day, 2e-6)
        arc_orders = {(o["ts_code"], o["action"]): o for o in d["orders"]}
        audit.check("archive orders", len(arc_orders) == len(orders[day]), day)
        for o in orders[day]:
            arc = arc_orders.get((o["code"], o["side"]))
            audit.check("archive order identity", bool(arc), f"{day} {o['code']}")
            if arc:
                for k, eps in (
                    ("amount", 0.011),
                    ("shares", 0.000002),
                    ("fee", 0.0001),
                    ("nav", 1e-7),
                ):
                    audit.near("archive order " + k, o[k], arc[k], day, eps)
                if "硬止损" in card["rationale"]:
                    audit.check("archived stop-sale reason", "止损" in arc["reason"], day)
        missing = refs[day] - {s["id"] for s in d["evidence"]}
        audit.check("published references", not missing, f"{day} {sorted(missing)}")
        source_f = linked[card["payload"]["research_id"]]
        arc_f = {(v["sector_name"], v["horizon_days"]): v for v in d["forecasts"]}
        audit.check("forecast archive rows", len(source_f) == len(arc_f), day)
        for fid in source_f:
            f = forecasts[fid]
            entry = arc_f.get((f["board"], f["horizon_days"]))
            audit.check("forecast archive ID", bool(entry), fid)
            if entry:
                audit.check(
                    "forecast frozen identity",
                    entry["proxy_code"] == f["proxy_code"]
                    and entry["target_date"] == str(f["target_date"]),
                    fid,
                )
                audit.near(
                    "forecast frozen probability", f["prob_up"], entry["up_probability"], fid
                )
                outcome = outcomes[fid]
                audit.check(
                    "post-run published label",
                    entry.get("after_replay_verified") is (fid in post),
                    fid,
                )
                if fid in post:
                    for k in ("verification_url", "verification_source", "verified_at"):
                        audit.check(
                            "published verification provenance",
                            entry.get(k) == post[fid][k],
                            f"{fid} {k}",
                        )
                    for k in ("base_close", "target_close"):
                        audit.near(
                            "published verified close", post[fid][k], entry.get(k), fid, 1e-8
                        )
                audit.check(
                    "published scoring status",
                    entry["score_status"] == ("scored" if outcome else "pending"),
                    fid,
                )
                audit.near(
                    "published outcome",
                    outcome["realized_return"] if outcome else None,
                    entry["actual_return"],
                    fid,
                )
                audit.check(
                    "published direction",
                    entry["hit"] == (outcome["hit"] if outcome else None),
                    fid,
                )
            exported_ids.add(fid)
    a = summary["account"]
    audit.check("benchmark anchor", a["benchmark_anchor"] == str(anchor), last)
    audit.near("archive initial capital", pod["initial_capital"], a["initial_capital"], last)
    for k in (
        "cash",
        "total_value",
        "cum_return",
        "max_drawdown",
        "benchmark_return",
        "benchmark_max_drawdown",
        "excess_return",
    ):
        audit.near(
            "account " + k,
            series[last][k],
            a[k],
            last,
            0.05 if k in ("cash", "total_value") else 2e-6,
        )
    for h in (1, 5):
        pred = [forecasts[fid] for fid in exported_ids if forecasts[fid]["horizon_days"] == h]
        verified = [
            {**f, **outcomes[f["forecast_id"]]}
            for f in pred
            if outcomes[f["forecast_id"]] is not None
        ]
        directional = [f for f in verified if f["hit"] is not None]
        hits = sum(f["hit"] for f in directional)
        correct = {
            "total": len(pred),
            "scored": len(verified),
            "directional": len(directional),
            "hits": hits,
            "neutral": len(verified) - len(directional),
            "pending": len(pred) - len(verified),
            "post_verified": sum(f["forecast_id"] in post for f in pred),
            "overdue": sum(
                f["target_date"] <= last for f in pred if outcomes[f["forecast_id"]] is None
            ),
            "not_due": sum(
                f["target_date"] > last for f in pred if outcomes[f["forecast_id"]] is None
            ),
            "baseline_up_rate": sum(f["realized_return"] > 0 for f in directional)
            / len(directional)
            if directional
            else None,
            "hit_rate": hits / len(directional) if directional else None,
            "brier": sum((f["prob_up"] - (f["realized_return"] > 0)) ** 2 for f in verified)
            / len(verified)
            if verified
            else None,
        }
        entry = next(x for x in summary["forecast_stats"] if x["horizon_days"] == h)
        for k, v in correct.items():
            audit.near("prediction summary " + k, v, entry.get(k), h, 1e-9)
    models = sum(d["mode"] == "llm" for d in visible)
    coverage = summary["coverage"]
    audit.check(
        "archive coverage",
        coverage["model_days"] == models
        and coverage["rule_days"] == completed - models
        and coverage["orders"] == sum(len(orders[d]) for d in sessions[:completed])
        and coverage["actions"] == dict(Counter(cards[d]["action"] for d in sessions[:completed])),
        last,
    )


def run(archive_path, require_complete=False):
    # The file is atomically replaced after the corresponding DB commit. Open it
    # before acquiring the SQL snapshot so even an actively growing run can be checked.
    archive = json.loads(Path(archive_path).read_bytes())
    audit = Audit()
    with db.transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        name = db.query_one("SELECT current_database() AS name")["name"]
        if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", name):
            raise ValueError("只读审计需要指定 quantlab_replay_* 独立数据库")
        t = db.qualified
        pods = db.query(f"SELECT * FROM {t('pod')}")
        if (
            len(pods) != 1
            or pods[0]["strategy_key"] != "agent_manager"
            or pods[0]["entry_evidence"].get("mode") != "historical_replay"
        ):
            raise ValueError("回放库账户标识不匹配")
        pod = pods[0]
        meta = pod["entry_evidence"]
        calendar = [
            r["trade_date"]
            for r in db.query(f"SELECT trade_date FROM {t('trade_calendar')} ORDER BY 1")
        ]
        sessions = [d for d in calendar if meta["start"] <= str(d) <= meta["end"]]
        anchor = calendar[calendar.index(sessions[0]) - 1]
        navs = db.query(f"SELECT * FROM {t('pod_nav')} ORDER BY trade_date")
        audit.check(
            "isolated ledger",
            meta["run_id"] == name and all(r["pod_id"] == pod["pod_id"] for r in navs),
            name,
        )
        audit.check(
            "settled session prefix", [r["trade_date"] for r in navs] == sessions[: len(navs)], name
        )
        if require_complete:
            audit.check("complete DB", len(navs) == len(sessions), name)
            audit.check("complete archive", len(archive["days"]) == len(sessions), name)
        prices = defaultdict(dict)
        for q in db.query(f"SELECT board_id,trade_date,close::float px FROM {t('board_quote')}"):
            prices[q["board_id"]][q["trade_date"]] = q["px"]
        for q in db.query(
            f"SELECT i.code,n.trade_date,n.nav::float px FROM {t('instrument_nav')} n JOIN {t('instrument')} i USING(instrument_id)"
        ):
            prices[q["code"]][q["trade_date"]] = q["px"]
        bench = {
            q["trade_date"]: q["px"]
            for q in db.query(
                f"SELECT trade_date,close::float px FROM {t('index_quote')} WHERE index_code=%s",
                [pod["benchmark_code"]],
            )
        }
        reports = {r["report_id"]: r for r in db.query(f"SELECT * FROM {t('agent_research')}")}
        news = {
            n["news_id"]: n
            for n in db.query(f"SELECT news_id,title,published_at,source,url FROM {t('news_item')}")
        }
        decisions = db.query(
            f"SELECT * FROM {t('pod_decision')} WHERE executed_at IS NOT NULL ORDER BY trade_date"
        )
        cards = {d["trade_date"]: d for d in decisions}
        audit.check(
            "settled decisions",
            len(decisions) == len(navs) and set(cards) == {n["trade_date"] for n in navs},
            name,
        )
        last = navs[-1]["trade_date"] if navs else sessions[0] - timedelta(days=1)
        last_clock = datetime.combine(last + timedelta(days=1), time(8, 30), SH)
        forecasts = db.query(f"SELECT * FROM {t('agent_board_forecast')}")
        pred_map = forecast_returns(audit, forecasts, calendar, prices, last_clock)
        supplement_table = t("replay_forecast_verification")
        exists = db.query_one("SELECT to_regclass(%s) IS NOT NULL AS present", [supplement_table])[
            "present"
        ]
        verification = (
            db.query(f"""
            SELECT v.*, b.source anchor_source, e.source end_source
            FROM {supplement_table} v
            JOIN {t("agent_board_forecast")} f USING(forecast_id)
            LEFT JOIN {t("board_quote")} b ON b.board_id=f.board_id AND b.trade_date=f.base_date
            LEFT JOIN {t("board_quote")} e ON e.board_id=f.board_id AND e.trade_date=f.target_date
        """)
            if exists
            else []
        )
        for v in verification:
            for key in ("anchor_source", "end_source"):
                if v[key] is not None:
                    audit.check("archived quote provider", v[key] == v["source"], v["forecast_id"])
        board_identity = {
            r["board_id"]: r
            for r in db.query(f"SELECT board_id,code,name,source FROM {t('board')}")
        }
        supplements = verify_supplements(
            audit, verification, pred_map, prices, board_identity, sessions, len(navs)
        )
        linked = defaultdict(list)
        for row in db.query(f"SELECT report_id,forecast_id FROM {t('agent_report_forecast')}"):
            linked[row["report_id"]].append(row["forecast_id"])
        orders = defaultdict(list)
        for row in db.query(
            f"SELECT o.*,i.code FROM {t('pod_order')} o JOIN {t('instrument')} i USING(instrument_id) ORDER BY o.order_id"
        ):
            orders[row["execute_on"]].append(row)
        positions = defaultdict(dict)
        for row in db.query(
            f"SELECT p.*,i.code FROM {t('pod_position')} p JOIN {t('instrument')} i USING(instrument_id)"
        ):
            positions[row["trade_date"]][row["code"]] = row
        refs = {}
        for day, card in cards.items():
            rid = card["payload"]["research_id"]
            r = reports[rid]
            audit.check(
                "frozen strategy version",
                meta.get("strategy_version", "v1")
                == r["payload"].get("strategy_version", "v1")
                == card["payload"].get("strategy_version", "v1"),
                day,
            )
            audit.check(
                "daily decisions",
                card["created_at"] == r["as_of"]
                and r["as_of"].astimezone(SH).date() == day
                and card["executed_at"]
                == datetime.combine(day + timedelta(days=1), time(8, 30), SH)
                and card["payload"]["mode"] == "replay",
                day,
            )
            refs[day] = audit_report(audit, r, calendar, prices, news, forecasts)
            # Each issued prediction must equal the candidate estimate the manager actually saw.
            issued = {
                (s["board_id"], f["horizon_days"]): (s, f)
                for s in r["payload"]["watchlist"]
                for f in s.get("outlooks", [])
            }
            audit.check("issued forecast count", len(issued) == len(linked[rid]), day)
            for fid in linked[rid]:
                actual = pred_map[fid]
                source = issued.get((actual["board_id"], actual["horizon_days"]))
                audit.check(
                    "issued forecast identity",
                    source
                    and source[0]["proxy_code"] == actual["proxy_code"]
                    and r["as_of"] == actual["issued_at"],
                    fid,
                )
                if source:
                    for field in ("samples", "expected_return", "prob_up"):
                        audit.near(
                            "published estimate " + field,
                            source[1][field],
                            actual[field],
                            fid,
                            1e-9,
                        )
        series = audit_ledger(
            audit,
            navs,
            orders,
            positions,
            cards,
            prices,
            bench,
            anchor,
            float(pod["initial_capital"]),
        )
        audit_archive(
            audit,
            archive,
            pod,
            sessions,
            series,
            cards,
            orders,
            refs,
            pred_map,
            linked,
            anchor,
            supplements,
        )
    return {
        "ok": audit.failed == 0,
        "database": name,
        "settled_days": len(navs),
        "archive_days": len(archive["days"]),
        "calendar_sessions": len(sessions),
        "forecasts": len(forecasts),
        "post_verified": len(supplements),
        "orders": sum(map(len, orders.values())),
        "stop_days": sum("硬止损" in c["rationale"] for c in cards.values()),
        "checks": dict(audit.checks),
        "failure_count": audit.failed,
        "failures": audit.examples,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DATA_DIR / "manager-replay" / "latest.json")
    parser.add_argument(
        "--require-complete", action="store_true", help="区间的每个交易日都必须在数据库和归档中"
    )
    args = parser.parse_args()
    result = run(args.archive, args.require_complete)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
