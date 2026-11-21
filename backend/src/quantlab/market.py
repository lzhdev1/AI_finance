"""Published market coverage and read-only, on-demand sector estimates."""

from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

from . import db, manager_v2

VERIFIED_FLOWS = ("ths-close-verified", "eastmoney-close-verified")
SOURCES = {"ths": "同花顺", "eastmoney": "东方财富"}


def flow_evidence(source, kind, day, net, pct, target, net_5d=None, pct_5d=None):
    provider = (source or "").removesuffix("-close-verified")
    url = None
    method = None
    if provider == "ths":
        url = "https://data.10jqka.com.cn/funds/" + ("hyzjl/" if kind == "industry" else "gnzjl/")
        method = "同花顺流入资金减流出资金，占比为净额除以流入与流出之和"
    elif provider == "eastmoney":
        url = (
            "https://data.eastmoney.com/bkzj/hy.html"
            if kind == "industry"
            else "https://data.eastmoney.com/bkzj/gn.html"
        )
        method = "东方财富主力净流入（超大单与大单），净占比按源站成交额口径"
    status = "missing"
    if day and net is not None:
        status = (
            "unverified"
            if source not in VERIFIED_FLOWS
            else "available"
            if day == target
            else "stale"
        )
    return {
        "flow_date": str(day) if day else None,
        "flow_source": SOURCES.get(provider),
        "flow_url": url,
        "flow_method": method,
        "net_yuan": float(net) if net is not None else None,
        "net_pct": float(pct) if pct is not None else None,
        "net_5d_yuan": float(net_5d) if net_5d is not None else None,
        "net_5d_pct": float(pct_5d) if pct_5d is not None else None,
        "flow_status": status,
    }


def _quote_url(source, kind, code):
    if source == "ths":
        return (
            f"https://q.10jqka.com.cn/{'thshy' if kind == 'industry' else 'gn'}/detail/code/{code}/"
        )
    if source == "eastmoney":
        return f"https://quote.eastmoney.com/bk/90.{code}.html"
    return None


def market_screen(kind):
    # Use one cut-off throughout the response and allow late-arriving data to
    # appear on the next minute's request without persisting any survey forecasts.
    return _screen(kind, datetime.now(ZoneInfo("Asia/Shanghai")).replace(second=0, microsecond=0))


@lru_cache(maxsize=4)
def _screen(kind, cutoff):
    from .behavior import LABELS
    from .research import _sessions

    closed, base, due = _sessions(cutoff)
    target = closed[-1] if closed else None
    calendar = closed[-220:]
    t = db.qualified
    boards = db.query(
        f"""SELECT b.board_id,b.code,b.name,b.kind,b.source,
                f.trade_date flow_date,f.main_net::float net,f.main_net_pct::float pct,f.source flow_source,
                w.main_net::float net_5d,w.main_net_pct::float pct_5d
            FROM {t("board")} b LEFT JOIN LATERAL (
                SELECT trade_date,main_net,main_net_pct,source FROM {t("board_fund_flow")}
                WHERE board_id=b.board_id AND period='1d' AND trade_date<=%s AND ingested_at<=%s
                ORDER BY trade_date DESC LIMIT 1) f ON true
            LEFT JOIN {t("board_fund_flow")} w ON w.board_id=b.board_id AND w.trade_date=f.trade_date
                AND w.period='5d' AND w.source=f.source AND w.source=ANY(%s) AND w.ingested_at<=%s
            WHERE b.is_active AND b.kind=%s ORDER BY b.board_id""",
        [target, cutoff, list(VERIFIED_FLOWS), cutoff, kind],
    )
    rows = db.query(
        f"""SELECT q.board_id,q.trade_date,q.close::float close,q.pct_chg::float pct,q.source
            FROM {t("board_quote")} q JOIN {t("board")} b USING(board_id)
            WHERE b.is_active AND b.kind=%s AND q.trade_date BETWEEN %s AND %s
            AND q.ingested_at<=%s AND q.close>0 ORDER BY q.board_id,q.trade_date""",
        [kind, calendar[0] if calendar else None, target, cutoff],
    )
    by_board = defaultdict(list)
    for row in rows:
        by_board[row["board_id"]].append(row)
    report = db.query_one(
        f"""SELECT as_of,data_as_of,payload->'watchlist' watchlist,
                   payload->'evidence'->'behavior_screen' behavior_screen FROM {t("agent_research")}
            WHERE as_of<=%s AND mode IN ('llm','rules') ORDER BY as_of DESC,report_id DESC LIMIT 1""",
        [cutoff],
    )
    focus = {r["board_id"] for r in (report or {}).get("watchlist") or []}
    screened = (report or {}).get("behavior_screen") or {}
    calibration = manager_v2.calibration(cutoff)
    items = []
    for board in boards:
        history = by_board[board["board_id"]]
        last = history[-1] if history else {}
        date = last.get("trade_date")
        source = last.get("source")
        pct = last.get("pct")
        if (
            pct is None
            and len(history) >= 2
            and len(calendar) >= 2
            and [r["trade_date"] for r in history[-2:]] == calendar[-2:]
        ):
            pct = 100 * (last["close"] / history[-2]["close"] - 1)
        outlooks = []
        if date and date == target:
            prices = pd.Series({r["trade_date"]: r["close"] for r in history}).reindex(calendar)
            for horizon, end in due.items():
                estimate = manager_v2.forecast(prices, horizon, calibration, lead_sessions=int(base != date))
                if estimate:
                    outlooks.append(
                        {
                            **estimate,
                            "horizon_days": horizon,
                            "base_date": str(base),
                            "target_date": str(end),
                        }
                    )
        status = (
            "stale"
            if date and date != target
            else "available"
            if len(outlooks) == 2
            else "partial"
            if outlooks
            else "insufficient"
        )
        item = {k: board[k] for k in ("board_id", "code", "name", "kind")}
        regime = screened.get(str(board["board_id"]))
        item.update(
            behavior_regime=regime,
            behavior_label=LABELS.get(regime),
            behavior_date=str(report["data_as_of"]) if regime else None,
            quote_date=str(date) if date else None,
            quote_source=SOURCES.get(source),
            quote_url=_quote_url(source, kind, board["code"]),
            close=last.get("close"),
            pct_chg=pct,
            tracked=board["board_id"] in focus,
            outlooks=outlooks,
            forecast_status=status,
        )
        item.update(
            flow_evidence(
                board["flow_source"],
                kind,
                board["flow_date"],
                board["net"],
                board["pct"],
                target,
                board["net_5d"],
                board["pct_5d"],
            )
        )
        items.append(item)
    return {
        "as_of": cutoff.isoformat(),
        "trade_date": str(target) if target else None,
        "kind": kind,
        "tracked_as_of": report["as_of"].isoformat() if report else None,
        "coverage": {
            "total": len(items),
            "quotes": sum(bool(target) and i["quote_date"] == str(target) for i in items),
            "flows": sum(i["flow_status"] == "available" for i in items),
            "outlooks": sum(bool(i["outlooks"]) for i in items),
            "tracked": sum(i["tracked"] for i in items),
        },
        "items": items,
    }
