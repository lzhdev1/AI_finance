"""Freeze sector observations before the next application, then score matured observations."""

import json
from collections import defaultdict
from datetime import date
from statistics import median

import pandas as pd

from . import config, db, opportunity


def realized_memories(observations, quotes, sessions):
    """Five full subsequent exchange sessions, once per non-overlapping board/state window."""
    positions = {day: i for i, day in enumerate(sessions)}
    outcomes, last_end = defaultdict(list), {}
    for bid, state, base in sorted(set(observations), key=lambda o: o[2]):
        i = positions.get(base)
        key = (bid, state)
        if i is None or i + 5 >= len(sessions) or (key in last_end and base <= last_end[key]):
            continue
        days = sessions[i : i + 6]
        points = [quotes.get((bid, d)) for d in days]
        if not all(points) or not all(
            p["close"] > 0 and p["low"] and p["high"] and 0 < p["low"] <= p["close"] <= p["high"]
            for p in points
        ):
            continue
        entry = points[0]["close"]
        future = points[1:]
        outcomes[key].append(
            (
                future[-1]["close"] / entry - 1,
                min(0, min(p["low"] for p in future) / entry - 1),
                max(0, max(p["high"] for p in future) / entry - 1),
            )
        )
        last_end[key] = days[-1]
    result = {}
    for key, rows in outcomes.items():
        n = len(rows)
        result[key] = {
            "samples": n,
            "up_rate": sum(r[0] > 0 for r in rows) / n if n >= 8 else None,
            "median_adverse_move": median(r[1] for r in rows) if n >= 8 else None,
            "median_favorable_move": median(r[2] for r in rows) if n >= 8 else None,
        }
    return result


def _memory(closed, as_of):
    if len(closed) < 6:
        return {}
    rows = db.query(
        f"""SELECT DISTINCT (s->>'board_id')::integer bid,s->>'state' state,
                   (o->>'application_date')::date base
            FROM {db.qualified("agent_research")} r
            CROSS JOIN LATERAL (SELECT payload->'evidence'->'opportunities' o) inputs
            CROSS JOIN LATERAL jsonb_array_elements(o->'signals') s
            WHERE payload->>'run_mode'='live' AND r.as_of<=%s
              AND (payload->>'recorded_at')::timestamptz<=%s
              AND (payload->>'recorded_at')::timestamptz <
                  (((o->>'application_date')::date + time '15:00') AT TIME ZONE 'Asia/Shanghai')
              AND (o->>'application_date')::date BETWEEN %s AND %s
              AND s->>'state' IN ('breakout','retest','distribution','breakdown')""",
        [as_of, as_of, closed[0], closed[-6]],
    )
    if not rows:
        return {}
    points = db.query(
        f"""SELECT q.board_id,q.trade_date,q.close::float,q.low::float,q.high::float
            FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
            WHERE q.source=b.source AND q.board_id=ANY(%s) AND q.trade_date BETWEEN %s AND %s
              AND q.ingested_at<=%s""",
        [list({r["bid"] for r in rows}), min(r["base"] for r in rows), closed[-1], as_of],
    )
    return realized_memories(
        [(r["bid"], r["state"], r["base"]) for r in rows],
        {(q["board_id"], q["trade_date"]): q for q in points},
        closed,
    )


def scan(candidates, closed, as_of, application_days, held):
    bars = opportunity.load_bars([r["board_id"] for r in candidates], closed, as_of)
    disclosures = json.loads(
        (config.CONFIG_DIR / "replay_fund_disclosures.json").read_text(encoding="utf-8")
    )
    mapped = {
        f["code"]: f
        for f in disclosures["funds"]
        if f["document_confirms_index"] and f["disclosed"] < as_of.date().isoformat()
    }
    memory = _memory(closed, as_of)
    signals, covered = [], 0
    for row in candidates:
        bid = row["board_id"]
        signal = opportunity.assess(
            bars.get(bid, []),
            closed_sessions=closed,
            as_of=as_of,
            flow=row.get("capital_flow"),
            intraday=row.get("intraday"),
        )
        covered += signal["state"] != "insufficient"
        if (
            signal["state"] in ("watch", "insufficient")
            and not signal["metrics"].get("extended")
            and not signal["metrics"].get("intraday_alert")
            and not any(f["code"] in held for f in row["funds"])
        ):
            continue
        funds = [
            {**f, "disclosure": mapped[f["code"]]["source_url"]}
            for f in row["funds"]
            if f["code"] in mapped and mapped[f["code"]]["mapped_sector"] == row["board"]
        ]
        signal.update(
            board=row["board"], board_id=bid, funds=funds, memory=memory.get((bid, signal["state"]))
        )
        signals.append(signal)
        row["opportunity"] = signal
        row["evidence_ids"].append(f"O{bid}")
    signals.sort(key=lambda s: (not s["eligible"], -s["score"], s["board_id"]))
    day = application_days[0] if application_days else None
    end = application_days[5] if len(application_days) > 5 else None
    codes = sorted(set(held) | {f["code"] for s in signals if s["eligible"] for f in s["funds"]})
    navs = opportunity.load_navs(codes, closed, as_of)
    # The fee document must already be public at the cutoff, including weekend research.
    schedules = opportunity.load_fees(codes, day=min(day, as_of.date()) if day else None)
    fees = {c: opportunity.fee_cost(schedules.get(c), day, end) for c in codes}
    best, rejected = opportunity.select_probe(
        signals, navs, fees, closed_sessions=closed, allocated=held
    )
    candidate = None
    if best:
        s, f, weight, reason = best
        candidate = {
            **f,
            "board": s["board"],
            "board_id": s["board_id"],
            "state": s["state"],
            "weight": weight,
            "reason": reason,
        }
    return {
        "version": 1,
        "boards_screened": len(candidates),
        "with_signal_data": covered,
        "application_date": str(day) if day else None,
        "evaluation_date": str(end) if end else None,
        "candidate": candidate,
        "rejections": rejected,
        "signals": signals,
        "closed_sessions": [str(d) for d in closed[-22:]],
        "round_trip_fees": fees,
        "fee_schedules": schedules,
        "fund_navs": {
            c: {str(d): float(v) for d, v in series.items()} for c, series in navs.items()
        },
    }


def plan(
    screen, account, held, targets, prices, day, previous, cash_receivable, allow_new, force_exit
):
    navs = {
        code: pd.Series({date.fromisoformat(d): v for d, v in points.items()}, dtype=float)
        for code, points in screen["fund_navs"].items()
    }
    equity = account.total_value(prices)
    return opportunity.apply_probe(
        targets,
        held,
        screen["signals"],
        navs,
        screen["round_trip_fees"],
        closed_sessions=screen["closed_sessions"],
        day=day,
        previous=previous,
        cash_weight=max(0, account.cash - cash_receivable) / equity if equity > 0 else 0,
        allow_new=allow_new,
        force_exit=force_exit,
    )


def missing_screen(day):
    dates = db.query(
        f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date<%s "
        "ORDER BY trade_date DESC LIMIT 22",
        [day],
    )
    return {
        "signals": [],
        "closed_sessions": [str(row["trade_date"]) for row in reversed(dates)],
        "round_trip_fees": {},
        "fund_navs": {},
        "fee_schedules": {},
    }


def monthly_turnover(pod_id, day):
    return db.query_one(
        f"""SELECT coalesce(sum(o.amount),0)::float total,
                   coalesce(sum(o.amount) FILTER (WHERE o.side='buy' AND EXISTS
                       (SELECT 1 FROM jsonb_array_elements(d.payload->'instructions') item
                        WHERE item->>'code'=i.code AND item->>'side'='buy'
                          AND item->>'opportunity_probe'='true')),0)::float probe_buys
            FROM {db.qualified("pod_order")} o
            JOIN {db.qualified("instrument")} i USING(instrument_id)
            LEFT JOIN {db.qualified("pod_decision")} d ON d.pod_id=o.pod_id AND d.trade_date=o.decided_on
            WHERE o.pod_id=%s AND o.execute_on BETWEEN %s AND %s""",
        [pod_id, day.replace(day=1), day],
    )
