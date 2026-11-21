"""Meter actual service attempts. Keep billed amounts, estimates and unpriced calls distinct."""

from __future__ import annotations

import logging
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

from . import db

log = logging.getLogger(__name__)
SH = ZoneInfo("Asia/Shanghai")
_scope = ContextVar("manager_usage_context", default=None)
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)
KINDS = ("llm", "search", "data", "tool")


def active():
    return _scope.get() is not None


@contextmanager
def scope(name, run_id=None, *, inherit=False):
    if inherit and active():
        yield
        return
    token = _scope.set((uuid4(), run_id, name))
    try:
        yield
    finally:
        _scope.reset(token)


def wrap(fn):
    """Copy only attribution into a worker thread, never its caller's DB transaction."""
    current = _scope.get()

    def run(*args, **kwargs):
        token = _scope.set(current)
        try:
            return fn(*args, **kwargs)
        finally:
            _scope.reset(token)

    return run


def attach_report(conn, report_id):
    if active():
        conn.execute(
            f"INSERT INTO {db.qualified('agent_usage_report')}(report_id,context_id) VALUES(%s,%s)",
            [report_id, _scope.get()[0]],
        )


def record_event(
    *,
    kind,
    provider,
    operation,
    status,
    model=None,
    input_tokens=None,
    output_tokens=None,
    total_tokens=None,
    cached_tokens=None,
    reasoning_tokens=None,
    amount=None,
    currency=None,
    cost_basis="unknown",
    price_note=None,
    source_url=None,
    detail=None,
):
    current = _scope.get()
    if not current:
        return
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if cost_basis == "no_charge":
        amount, currency = 0, None
    elif cost_basis == "unknown":
        amount, currency = None, None
    # Calls remain billable even when the outer strategy transaction later rolls back.
    # Do not hold a writer or a DB transaction open while waiting on external services.
    try:
        with psycopg.connect(
            db.dsn(),
            connect_timeout=2,
            autocommit=True,
            options="-c statement_timeout=1500",
            application_name="manager_usage",
        ) as conn:
            conn.execute(
                f"""INSERT INTO {db.qualified("agent_usage_event")}
                (context_id,run_id,scope,kind,provider,operation,model,status,input_tokens,output_tokens,
                 total_tokens,cached_tokens,reasoning_tokens,cost_basis,amount,currency,price_note,source_url,detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    *current,
                    kind,
                    provider,
                    operation,
                    model,
                    status,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cached_tokens,
                    reasoning_tokens,
                    cost_basis,
                    amount,
                    currency,
                    price_note,
                    source_url,
                    detail,
                ],
            )
    except psycopg.Error as exc:
        log.warning("用量记录写入失败（%s，%s）", operation, type(exc).__name__)


@contextmanager
def attempt(**fields):
    try:
        yield
    except Exception:
        record_event(status="error", **fields)
        raise
    else:
        record_event(status="success", **fields)


def _totals(rows):
    result = dict(calls=0, errors=0, missing_token_calls=0, unknown_cost_calls=0, no_charge_calls=0)
    result.update({f"{k}_calls": 0 for k in KINDS})
    result.update(dict.fromkeys(TOKEN_FIELDS))
    costs = {}
    for row in rows:
        calls, basis = row["calls"], row["cost_basis"]
        result["calls"] += calls
        result["errors"] += row["errors"]
        result[f"{row['kind']}_calls"] += calls
        result["missing_token_calls"] += row["missing"]
        for key in TOKEN_FIELDS:
            if row[key] is not None:
                result[key] = (result[key] or 0) + int(row[key])
        if basis == "unknown":
            result["unknown_cost_calls"] += calls
        elif basis == "no_charge":
            result["no_charge_calls"] += calls
        else:
            currency = row["currency"]
            c = costs.setdefault(
                currency,
                dict(
                    currency=currency,
                    reported_amount=None,
                    estimated_amount=None,
                    reported_calls=0,
                    estimated_calls=0,
                ),
            )
            c[f"{basis}_calls"] += calls
            c[f"{basis}_amount"] = (c[f"{basis}_amount"] or Decimal(0)) + row["amount"]
    result["costs"] = [costs[c] for c in sorted(costs)]
    for c in result["costs"]:
        for key in ("reported_amount", "estimated_amount"):
            if c[key] is not None:
                c[key] = format(c[key], "f")
    return result


def _aggregate(predicate="", params=()):
    return db.query(
        f"""SELECT e.kind,e.provider,e.model,e.cost_basis,e.currency,count(*) calls,
        count(*) FILTER(WHERE status='error') errors,
        count(*) FILTER(WHERE kind='llm' AND total_tokens IS NULL) missing,
        {",".join(f"sum({k}) AS {k}" for k in TOKEN_FIELDS)},sum(amount) amount
        FROM {db.qualified("agent_usage_event")} e {predicate}
        GROUP BY e.kind,e.provider,e.model,e.cost_basis,e.currency""",
        params,
    )


def replay_snapshot():
    if not db.query_one("SELECT to_regclass(%s) AS name", [db.qualified("agent_usage_event")])[
        "name"
    ]:
        return None
    count = db.query_one(f"""SELECT count(*) n FROM {db.qualified("agent_usage_report")} l
        JOIN {db.qualified("agent_research")} r USING(report_id)
        WHERE r.payload->>'run_mode'='historical_replay'""")["n"]
    rows = _aggregate("WHERE e.scope='historical_replay'")
    if not count and not rows:
        return None
    return {"summary": _totals(rows), "tracked_reports": count}


def dashboard(days="30", report_id=None, kind="all", offset=0, limit=30, now=None):
    now = (now or datetime.now(SH)).astimezone(SH)
    t = db.qualified
    where, params = [], []
    if report_id is not None:
        where.append(
            "e.context_id=(SELECT context_id FROM "
            + t("agent_usage_report")
            + " WHERE report_id=%s)"
        )
        params.append(report_id)
    elif days != "all":
        first = now.date() - timedelta(days=int(days) - 1)
        where.append("e.occurred_at >= %s AND e.occurred_at < %s")
        params.extend(
            [
                datetime.combine(first, time.min, SH),
                datetime.combine(now.date() + timedelta(days=1), time.min, SH),
            ]
        )
    if kind != "all":
        where.append("e.kind=%s")
        params.append(kind)
    predicate = " WHERE " + " AND ".join(where) if where else ""
    table = t("agent_usage_event")
    with db.transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        started = db.query_one(f"SELECT started_at FROM {t('usage_tracking')} WHERE singleton")[
            "started_at"
        ]
        report, history = None, []
        if report_id is not None:
            report = db.query_one(
                f"""SELECT r.as_of,l.context_id IS NOT NULL AS tracked,
                CASE WHEN l.context_id IS NULL THEN r.payload#>'{{evidence,research_tools,calls}}' END AS tools
                FROM {t("agent_research")} r LEFT JOIN {t("agent_usage_report")} l USING(report_id)
                WHERE r.report_id=%s""",
                [report_id],
            )
            if not report:
                raise LookupError("研究报告不存在")
            known = {
                "read_news": ("search", "检索板块新闻"),
                "inspect_sector": ("tool", "查阅已载入板块指标"),
                "recall": ("tool", "检索历史经验"),
            }
            counts = Counter(
                c.get("tool")
                for c in (report["tools"] or [])
                if isinstance(c, dict)
                and c.get("status") in ("ok", "empty")
                and c.get("tool") in known
            )
            history = [
                dict(operation=known[k][1], kind=known[k][0], provider="本地研究库", count=n)
                for k, n in sorted(counts.items())
                if kind in ("all", known[k][0])
            ]
        rows = _aggregate(predicate, params)
        events = db.query(
            f"""SELECT e.id,e.occurred_at,e.run_id,l.report_id,e.scope,e.kind,e.provider,
            e.operation,e.model,e.status,{",".join("e." + k for k in TOKEN_FIELDS)},e.cost_basis,
            e.amount::text,e.currency,e.price_note,e.source_url,e.detail
            FROM {table} e LEFT JOIN {t("agent_usage_report")} l USING(context_id){predicate}
            ORDER BY e.occurred_at DESC,e.id DESC LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    groups = {}
    for r in rows:
        groups.setdefault((r["kind"], r["provider"], r["model"]), []).append(r)
    summary = _totals(rows)
    return dict(
        tracking_started_at=started,
        days=days,
        report_id=report_id,
        report_as_of=report["as_of"] if report else None,
        report_coverage=("tracked" if report["tracked"] else "not_tracked") if report else None,
        historical_tools=history,
        summary=summary,
        groups=sorted(
            (
                dict(kind=k, provider=p, model=m, **_totals(group))
                for (k, p, m), group in groups.items()
            ),
            key=lambda g: (-g["calls"], g["provider"], g["model"] or ""),
        ),
        events=events,
        total=summary["calls"],
        offset=offset,
        limit=limit,
    )
