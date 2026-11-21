"""Point-in-time predictions of selected funds' own published unit NAVs."""

from collections import defaultdict
from datetime import datetime

import pandas as pd
from psycopg.types.json import Jsonb

from . import db, manager_v2, otc


def _selection(watch, targets, account):
    reasons = defaultdict(list)
    for item in (account or {}).get("holdings", []):
        reasons[item["code"]].append("账户持仓")
    for code, weight in (targets or {}).items():
        if weight > 0:
            reasons[code].append("目标持仓")
    for board in watch or []:
        for fund in board.get("funds", []):
            reason = f"{board.get('board', '跟踪板块')}的映射基金"
            if reason not in reasons[fund["code"]]:
                reasons[fund["code"]].append(reason)
    return reasons


def selected_for_report(rows, watch, targets, account):
    reasons = _selection(watch, targets, account)
    return [
        dict(row, selected_by="、".join(reasons[row["code"]]))
        for row in rows
        if row["code"] in reasons
    ]


def _calibration(codes, as_of):
    history = db.query(
        f"""WITH resolved AS (
            SELECT instrument_id,horizon_days,base_date,target_date,
                   (realized_return > 0)::int - (evidence->>'raw_prob_up')::float error,
                   row_number() OVER (PARTITION BY instrument_id,horizon_days ORDER BY target_date DESC) seq
            FROM {db.qualified("agent_fund_forecast")} f JOIN {db.qualified("instrument")} i USING(instrument_id)
            WHERE code=ANY(%s) AND scored_at <= %s AND evidence ? 'raw_prob_up'
        ) SELECT * FROM resolved WHERE seq<=%s*horizon_days ORDER BY target_date DESC""",
        [codes, as_of, manager_v2.CALIBRATION_DAYS],
    )
    calibration = defaultdict(dict)
    previous_base = {}
    errors = defaultdict(list)
    for row in history:
        key = (row["instrument_id"], row["horizon_days"])
        if len(errors[key]) == manager_v2.CALIBRATION_DAYS or (
            key in previous_base and row["target_date"] > previous_base[key]
        ):
            continue
        previous_base[key] = row["base_date"]
        errors[key].append((row["target_date"], row["error"]))
    for (instrument_id, horizon), sample in errors.items():
        residual = sum(error for _, error in sample)
        calibration[instrument_id][horizon] = {
            "days": len(sample),
            "latest": str(sample[0][0]),
            "shift": max(-0.1, min(0.1, residual / (len(sample) + manager_v2.CALIBRATION_PRIOR))),
        }
    return calibration


def _card(row):
    return row["evidence"] | {
        "id": row["forecast_id"],
        "report_id": row["report_id"],
        "status": "scored" if row["scored_at"] else "pending",
        "base_nav": float(row["base_nav"]) if row["base_nav"] is not None else None,
        "target_nav": float(row["target_nav"]) if row["target_nav"] is not None else None,
        "realized_return": row["realized_return"],
        "hit": row["hit"],
        "scored_at": row["scored_at"].isoformat() if row["scored_at"] else None,
    }


def prepare(as_of, closed, base, due, watch, targets, account):
    reasons = _selection(watch, targets, account)
    codes = sorted(reasons)
    if not codes:
        return []
    sample_days = list(closed[-250:])
    table, instrument = db.qualified("instrument_nav"), db.qualified("instrument")
    catalog = {
        r["code"]: r
        for r in db.query(
            f"SELECT code,name,instrument_id FROM {instrument} WHERE code=ANY(%s) AND kind='otc'",
            [codes],
        )
    }
    observations = defaultdict(dict)
    if sample_days:
        for row in db.query(
            f"SELECT i.code,n.trade_date,n.nav::float,n.ingested_at,n.source FROM {table} n "
            f"JOIN {instrument} i USING(instrument_id) WHERE code=ANY(%s) AND n.trade_date=ANY(%s) "
            "AND n.ingested_at<=%s AND n.nav>0 ORDER BY n.trade_date",
            [codes, sample_days, as_of],
        ):
            observations[row["code"]][row["trade_date"]] = row
    locked = {}
    if base:
        for row in db.query(
            f"SELECT f.* FROM {db.qualified('agent_fund_forecast')} f JOIN {instrument} i USING(instrument_id) "
            "WHERE code=ANY(%s) AND base_date=%s AND issued_at<=%s",
            [codes, base, as_of],
        ):
            card = _card(row)
            # The outcome has its own publication clock, independent of first issue.
            if row["scored_at"] and row["scored_at"] > as_of:
                card.update(
                    status="pending",
                    hit=None,
                    scored_at=None,
                    realized_return=None,
                    base_nav=None,
                    target_nav=None,
                )
            locked[card["code"], card["horizon_days"]] = card
    calibration = _calibration(codes, as_of)
    application_days = otc.next_application_sessions(as_of)
    results = []
    for code in codes:
        fund = catalog.get(code, {})
        navs = observations[code]
        observed = max(navs) if navs else None
        latest = navs.get(observed, {})
        # Count actual exchange sessions, preserving holes in the NAV series.
        lag = (
            sum(d > observed for d in sample_days) + int(bool(base and base not in sample_days))
            if observed
            else None
        )
        index = [d for d in sample_days if observed and d <= observed]
        series = pd.Series({d: row["nav"] for d, row in navs.items()}, dtype=float).reindex(index)
        for horizon in (1, 5):
            target = due.get(horizon)
            common = {
                "selected_by": "、".join(reasons[code]),
                "otc": otc.clock(code, application_days),
            }
            old = locked.get((code, horizon))
            if old:
                results.append(old | common)
                continue
            row = {
                "id": None,
                "report_id": None,
                "issued_at": as_of.isoformat(),
                "instrument_id": fund.get("instrument_id"),
                "code": code,
                "name": fund.get("name", code),
                "horizon_days": horizon,
                "base_date": str(base) if base else None,
                "target_date": str(target) if target else None,
                "observed_nav_date": str(observed) if observed else None,
                "observed_nav": latest.get("nav"),
                "observed_at": latest["ingested_at"].isoformat() if latest else None,
                "observation_lag": lag,
                "source": latest.get("source"),
                "unit": "元/份",
                "return_unit": "decimal",
                "source_url": (
                    f"https://fundf10.eastmoney.com/jjjz_{code}.html"
                    if str(latest.get("source", "")).startswith("eastmoney")
                    else None
                ),
                "prob_up": None,
                "expected_return": None,
                "confidence": None,
                "samples": 0,
                "direction": None,
                "status": "insufficient",
                "base_nav": None,
                "target_nav": None,
                "realized_return": None,
                "hit": None,
                "scored_at": None,
                **common,
            }
            if not base or not target:
                row["reason"] = "交易日历缺少基准日或到期日。"
            elif lag is None or lag > 1:
                row["reason"] = "未收到基准交易日或其前一交易日可见的基金单位净值。"
            else:
                estimate = manager_v2.forecast(
                    series, horizon, calibration[fund["instrument_id"]], lead_sessions=lag
                )
                if not estimate or estimate["samples"] < 3:
                    row.update(
                        reason="基金自身同期限已完成区间或同类趋势样本不足。",
                        samples=(estimate or {}).get("samples", 0),
                    )
                else:
                    p = estimate["prob_up"]
                    row.update(
                        estimate,
                        status="pending",
                        direction="up" if p > 0.5 else "down" if p < 0.5 else "neutral",
                        reason=f"基于本基金可见单位净值的5日、20日趋势，有{estimate['samples']}个同类已完成区间；"
                        f"预测从{base}到{target}单位净值的涨跌，按基金自身已核验误差收缩校准。",
                    )
            results.append(row)
    return results


def persist(report_id, rows, as_of):
    table, refs = db.qualified("agent_fund_forecast"), db.qualified("agent_report_fund_forecast")
    with db.transaction():
        for row in rows:
            if row["status"] == "insufficient":
                continue  # The report payload freezes the abstention and its reason.
            args = [row["instrument_id"], row["base_date"], row["horizon_days"]]
            db.execute(
                f"INSERT INTO {table}(report_id,issued_at,instrument_id,base_date,horizon_days,target_date,evidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(instrument_id,base_date,horizon_days) DO NOTHING",
                [report_id, as_of, *args, row["target_date"], Jsonb(row)],
            )
            db.execute(
                f"INSERT INTO {refs} SELECT %s,forecast_id FROM {table} WHERE instrument_id=%s "
                "AND base_date=%s AND horizon_days=%s AND target_date=%s AND issued_at<=%s ON CONFLICT DO NOTHING",
                [report_id, *args, row["target_date"], as_of],
            )


def missing_nav_dates(as_of):
    """Old gaps and watched-but-unheld funds also need the original NAV endpoints."""
    rows = db.query(
        f"""SELECT i.code,min(endpoint.day) missing_day
            FROM {db.qualified("agent_fund_forecast")} f JOIN {db.qualified("instrument")} i USING(instrument_id)
            CROSS JOIN LATERAL (VALUES(f.base_date),(f.target_date)) endpoint(day)
            WHERE f.scored_at IS NULL AND f.issued_at<=%s
              AND (endpoint.day + time '15:00') AT TIME ZONE 'Asia/Shanghai' <= %s
              AND NOT EXISTS(SELECT 1 FROM {db.qualified("instrument_nav")} n
                  WHERE n.instrument_id=f.instrument_id AND n.trade_date=endpoint.day AND n.nav>0
                    AND n.ingested_at<=%s)
            GROUP BY i.code""",
        [as_of, as_of, as_of],
    )
    return {r["code"]: r["missing_day"] for r in rows}


def settle(as_of: datetime):
    table, nav = db.qualified("agent_fund_forecast"), db.qualified("instrument_nav")
    return db.execute(
        f"""UPDATE {table} f SET base_nav=a.nav,target_nav=b.nav,realized_return=(b.nav/a.nav-1)::float,
            hit=CASE WHEN f.evidence->>'direction'='neutral' OR b.nav=a.nav THEN NULL
                     WHEN f.evidence->>'direction'='up' THEN b.nav>a.nav ELSE b.nav<a.nav END,
            scored_at=%s
            FROM {nav} a,{nav} b WHERE f.scored_at IS NULL AND f.issued_at<=%s
            AND (f.target_date + time '15:00') AT TIME ZONE 'Asia/Shanghai' <= %s
            AND a.instrument_id=f.instrument_id AND a.trade_date=f.base_date AND a.ingested_at<=%s
            AND b.instrument_id=f.instrument_id AND b.trade_date=f.target_date AND b.ingested_at<=%s
            AND a.nav>0 AND b.nav>0""",
        [as_of] * 5,
    )


def for_report(report_id):
    report = db.query_one(
        f"SELECT payload->'fund_forecasts' rows FROM {db.qualified('agent_research')} WHERE report_id=%s",
        [report_id],
    )
    if not report or not report["rows"]:
        return []
    actual = {
        _key(row["evidence"]): _card(row)
        for row in db.query(
            f"SELECT f.* FROM {db.qualified('agent_fund_forecast')} f JOIN {db.qualified('agent_report_fund_forecast')} r "
            "USING(forecast_id) WHERE r.report_id=%s",
            [report_id],
        )
    }
    cards = []
    for original in report["rows"]:
        if original["status"] == "insufficient":
            cards.append(original | {"report_id": report_id})
        elif _key(original) in actual:
            cards.append(actual[_key(original)] | {k: original[k] for k in ("selected_by", "otc")})
    return cards


def _key(row):
    return row["code"], row["base_date"], row["horizon_days"]


def history(offset=0, limit=40, horizon=None, status=None):
    # Unissued estimates live in the original research report so repeated
    # attempts never consume the unique, immutable slot for a real forecast.
    result = db.query(
        f"""WITH cards AS (
            SELECT f.issued_at,f.report_id,f.horizon_days,f.evidence->>'code' code,
                   CASE WHEN scored_at IS NULL THEN 'pending' ELSE 'scored' END status,
                   f.evidence || jsonb_build_object('id', forecast_id,'report_id',f.report_id,
                       'base_nav',base_nav,'target_nav',target_nav,'realized_return',realized_return,
                       'hit', hit,'scored_at',scored_at) card
            FROM {db.qualified("agent_fund_forecast")} f
            UNION ALL
            SELECT r.as_of,r.report_id,(v->>'horizon_days')::int,v->>'code','insufficient',
                   v || jsonb_build_object('report_id',r.report_id)
            FROM {db.qualified("agent_research")} r CROSS JOIN LATERAL
            jsonb_array_elements(coalesce(r.payload->'fund_forecasts','[]'::jsonb)) v
            WHERE v->>'status'='insufficient'
        ) SELECT card,status FROM cards WHERE (%s::int IS NULL OR horizon_days=%s)
          AND (%s::text IS NULL OR status=%s)
          ORDER BY issued_at DESC,report_id DESC,code,horizon_days OFFSET %s LIMIT %s""",
        [horizon, horizon, status, status, offset, limit + 1],
    )
    return {
        "items": [r["card"] | {"status": r["status"]} for r in result[:limit]],
        "has_more": len(result) > limit,
    }
