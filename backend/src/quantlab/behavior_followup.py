"""Recover an earlier published sector path and measure only its observable outcomes."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import db, market

SH = ZoneInfo("Asia/Shanghai")


def _plans(ids, days, as_of):
    # Filter inside PostgreSQL: research reports also include a much larger
    # unpublished candidate set, which must never masquerade as a prior call.
    rows = db.query(
        f"""SELECT r.report_id,r.as_of,(w->>'board_id')::int board_id,w->>'board' board,
                   w->'behavior'->>'regime' prior_regime,w->'behavior'->>'label' label,
                   w->'capital_flow'->>'flow_source' prior_flow_source,
                   w->>'source' price_source,w->>'proxy_code' proxy_code,step
            FROM {db.qualified("agent_research")} r CROSS JOIN LATERAL
            jsonb_array_elements(CASE WHEN jsonb_typeof(r.payload->'watchlist')='array'
                                      THEN r.payload->'watchlist' ELSE '[]'::jsonb END) w CROSS JOIN LATERAL
            jsonb_array_elements(CASE WHEN jsonb_typeof(w->'behavior'->'path')='array'
                                      THEN w->'behavior'->'path' ELSE '[]'::jsonb END) step
            WHERE r.as_of >= %s AND r.as_of < %s AND r.payload->>'run_mode'='live'
              AND coalesce((r.payload->>'recorded_at')::timestamptz,r.as_of)<=%s
              AND w->>'board_id'=ANY(%s) AND step->>'target_date'=%s
            ORDER BY r.as_of,r.report_id""",
        [datetime.combine(days[0], time.min, SH), as_of, as_of, [str(i) for i in ids], str(days[-1])],
    )
    selected = {}
    first_for_anchor = set()
    for row in rows:
        step = row["step"]
        ordinal = step.get("day")
        if type(ordinal) is not int or not 1 <= ordinal < len(days):
            continue
        issued = row["as_of"].astimezone(SH)
        anchor = days[-ordinal - 1]
        issued_sessions = [
            d
            for d in days
            if d < issued.date() or (d == issued.date() and issued.time() >= time(9, 30))
        ]
        if not issued_sessions or issued_sessions[-1] != anchor or not row["prior_regime"]:
            continue
        key = (row["board_id"], anchor)
        if key in first_for_anchor:
            continue
        first_for_anchor.add(key)
        # Prefer a falsifiable plan, then the latest baseline; the earliest
        # publication wins within one baseline, independent of subsequent returns.
        rank = (row["prior_regime"] != "insufficient", anchor)
        old = selected.get(row["board_id"])
        if old is None or rank > old[0]:
            selected[row["board_id"]] = (rank, row, anchor)
    return selected


def attach(pool, as_of, base):
    for candidate in pool:
        candidate["prior_behavior"] = None
    if not base or not pool:
        return
    if as_of.tzinfo is None:
        raise ValueError("行为计划回看需要带时区的研究时刻")
    cutoff = as_of.astimezone(SH)
    table = db.qualified
    days = [
        r["trade_date"]
        for r in db.query(
            f"SELECT trade_date FROM {table('trade_calendar')} WHERE trade_date<=%s AND ingested_at<=%s ORDER BY trade_date DESC LIMIT 6",
            [base, cutoff],
        )
    ][::-1]
    if len(days) < 2 or days[-1] != base:
        return
    plans = _plans([r["board_id"] for r in pool], days, cutoff)
    if not plans:
        return
    visible = [d for d in days if datetime.combine(d, time(15), SH) <= cutoff]
    # Materialize each actual exchange session even if either data source missed it.
    rows = db.query(
        f"""SELECT b.board_id,b.source expected_source,c.trade_date,q.close::float,q.source,
                   q.ingested_at price_at,f.main_net::float net_yuan,f.source flow_source,f.ingested_at flow_at
            FROM {table("board")} b CROSS JOIN {table("trade_calendar")} c
            LEFT JOIN {table("board_quote")} q ON q.board_id=b.board_id AND q.source=b.source
                AND q.trade_date=c.trade_date AND q.close>0 AND q.ingested_at<=%s
            LEFT JOIN {table("board_fund_flow")} f ON f.board_id=b.board_id AND f.trade_date=c.trade_date
                AND f.period='1d' AND f.source=ANY(%s) AND f.ingested_at<=%s
            WHERE b.board_id=ANY(%s) AND c.trade_date=ANY(%s) ORDER BY c.trade_date""",
        [cutoff, list(market.VERIFIED_FLOWS), cutoff, list(plans), visible],
    )
    observations = {}
    for row in rows:
        observations.setdefault(row["board_id"], {})[row["trade_date"]] = row
    for candidate in pool:
        board_id = candidate["board_id"]
        if board_id not in plans:
            continue
        _, plan, anchor = plans[board_id]
        candidate["prior_behavior"] = _review(
            plan, anchor, base, observations.get(board_id, {}), base in visible
        )


def _review(plan, anchor, checkpoint, days, closed):
    step = plan["step"]
    origin = days.get(anchor, {})
    # A proxy NAV can price the archived forecast, but not this sector observation.
    expected_source = origin.get("expected_source")
    same_series = (
        plan.get("proxy_code")
        or not plan.get("price_source")
        or plan["price_source"] == expected_source
    )
    seen = [row for day, row in days.items() if anchor < day <= checkpoint]
    last = next((r for r in reversed(seen) if r["close"] is not None), {})
    base_close = origin.get("close") if same_series else None
    observed = last.get("trade_date") if base_close else None
    realized = last["close"] / base_close - 1 if observed else None
    prior_provider = plan.get("prior_flow_source")
    flow = next(
        (
            r
            for r in reversed(seen)
            if r["net_yuan"] is not None
            and (
                not prior_provider
                or market.SOURCES.get(r["flow_source"].removesuffix("-close-verified"))
                == prior_provider
            )
        ),
        {},
    )
    if observed == checkpoint:
        status, note = "observed", "观察日的同源板块收盘已公布。"
    elif observed:
        status, note = "partial", f"阶段收益截至 {observed}；观察日收盘尚待核验。"
    elif not closed:
        status, note = "pending_close", "当期观察日尚未收盘，暂没有可计算的后续同源收盘收益。"
    else:
        status, note = "missing_data", "基准日或后续观察日缺少当时可见的同源板块收盘。"
    if not flow:
        note += " 观察期内暂无可比口径的已核验收盘净额。"
    elif flow["trade_date"] != checkpoint:
        note += f" 可比口径的收盘净额最近更新至 {flow['trade_date']}。"
    return {
        "report_id": plan["report_id"],
        "board": plan["board"],
        "prior_regime": plan["prior_regime"],
        "label": plan["label"],
        "prior_as_of": plan["as_of"].isoformat(),
        "checkpoint_date": str(checkpoint),
        "anchor_date": str(anchor),
        "day": step["day"],
        "watch": step.get("watch"),
        "confirm": step.get("confirm"),
        "invalidate": step.get("invalidate"),
        "realized_board_return": realized,
        "observed_close_date": str(observed) if observed else None,
        "anchor_close": base_close,
        "observed_close": last.get("close") if observed else None,
        "close_source": expected_source if base_close else None,
        "close_observed_at": last["price_at"].isoformat() if observed else None,
        "flow_date": str(flow["trade_date"]) if flow else None,
        "net_yuan": flow.get("net_yuan"),
        "flow_source": flow.get("flow_source"),
        "flow_status": "available" if flow else "missing",
        "flow_observed_at": flow["flow_at"].isoformat() if flow else None,
        "status": status,
        "note": note,
    }
