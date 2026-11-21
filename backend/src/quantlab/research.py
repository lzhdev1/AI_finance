"""Point-in-time sector research for the paper manager."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb

from . import (
    behavior_followup,
    behavior_policy,
    db,
    flow_claims,
    fund_forecasts,
    llm,
    manager_v2,
    market,
    opportunity_research,
    otc,
    pod_store,
    research_tools,
    usage,
)
from .config import load_settings
from .costs import holding_rate

SH = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 5)
REPLAY_METHOD = (
    "14:30 决策仅用前一交易日及更早的日线和净值，新闻以原始发布时间为界。"
    "历史回填保留实际采集时戳；可见性按历史市场日期重建。板块指数收盘后可评分，"
    "净值代理假设在次日 08:30 可用。基金范围、行业映射和修订后的数据可能含幸存者与修订偏差。"
    "模型使用当时新闻与真实行业名，不提供绝对回放日；模型训练记忆仍可能影响判断。"
)
FORECAST_METHOD = "同板块、同方向五日动量的历史收益频率。加入十个中性先验样本，五日窗口互不重叠。均值收益与上涨频率分别估计。"


def _clock(as_of):
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("as_of 必须包含时区")
    return as_of.astimezone(SH)


def _sessions(as_of, *, replay=False):
    day = as_of.date()
    days = [
        r["trade_date"]
        for r in db.query(
            f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date BETWEEN %s AND %s "
            "AND (%s OR ingested_at<=%s) ORDER BY 1",
            [day - timedelta(days=400), day + timedelta(days=35), replay, as_of],
        )
    ]
    closed = [d for d in days if d < day or (d == day and as_of.time() >= time(15))]
    # During a session the remaining intraday return is excluded. The first
    # one-session forecast is from today's close to the following close.
    base = day if day in days and as_of.time() >= time(9, 30) else (closed[-1] if closed else None)
    future = [d for d in days if base and d > base]
    return closed, base, {h: future[h - 1] for h in HORIZONS if len(future) >= h}


def forecast_window(as_of, base, due):
    return {
        "information_cutoff": as_of.isoformat(),
        "base_date": base.isoformat() if base else None,
        "next_session": due[1].isoformat() if 1 in due else None,
        "fifth_session": due[5].isoformat() if 5 in due else None,
        "definition": "从基准交易日收盘至下一及第五个交易日收盘分别判断涨跌。板块使用同一指数收盘价，"
        "标有代理代码的板块及单只基金使用对应基金的单位净值。五日指累计涨跌。"
        "盘中时基准日收盘价和单位净值尚未知，到期公布后再检验。",
    }


def decision_evidence_since(start, through, *, exclude_report_id=None):
    """Categories of newly visible decision inputs in (start, through], in one DB snapshot."""
    start, through = _clock(start), _clock(through)
    if through < start:
        raise ValueError("复核时点早于研究信息截止时间")
    # Availability of a news item is the later of publisher and local receipt clocks.
    # Historical bars are usable only after their exchange close and local ingestion.
    close = "(trade_date + time '15:00') AT TIME ZONE 'Asia/Shanghai'"
    inputs = (
        ("新闻", "news_item", "greatest(published_at,ingested_at)", "published_at >= w.until - interval '7 days'"),
        ("盘中量价及资金", "board_spot_observation", "captured_at",
         "source_at BETWEEN w.until - interval '30 minutes' AND captured_at"),
        ("板块收盘行情", "board_quote", f"greatest(ingested_at,{close})", "true"),
        ("板块收盘资金", "board_fund_flow", f"greatest(ingested_at,{close})",
         "source IN ('ths-close-verified','eastmoney-close-verified')"),
        ("沪深300行情", "index_quote", f"greatest(ingested_at,{close})", "index_code='000300'"),
        ("基金净值", "instrument_nav", f"greatest(ingested_at,{close})", "true"),
        ("交易日历", "trade_calendar", "ingested_at", "true"),
        ("板块目录", "board", "created_at", "true"),
        ("基金目录", "instrument", "updated_at", "true"),
        ("基金映射", "instrument_board", "updated_at", "true"),
        ("已验证板块预测", "agent_board_forecast", "scored_at", "true"),
        ("已验证基金预测", "agent_fund_forecast", "scored_at", "true"),
        ("账户成交", "pod_order", "created_at", "true"),
        ("账户结算", "pod_decision", "executed_at", "true"),
        ("已完成研究", "agent_research", "coalesce((payload->>'recorded_at')::timestamptz,as_of)",
         "report_id IS DISTINCT FROM w.excluded AND payload->>'run_mode'='live'"),
    )
    checks = [
        f"SELECT '{label}' category FROM w WHERE EXISTS (SELECT 1 FROM {db.qualified(table)} "
        f"WHERE ({stamp})>w.since AND ({stamp})<=w.until AND ({eligible}))"
        for label, table, stamp, eligible in inputs
    ]
    rows = db.query(
        "WITH w AS (SELECT %s::timestamptz since,%s::timestamptz until,%s::bigint excluded) "
        + " UNION ALL ".join(checks), [start, through, exclude_report_id],
    )
    return [r["category"] for r in rows]


def _news(as_of, replay=False):
    with usage.attempt(kind="search", provider="本地研究库", operation="筛选近期已入库新闻",
                       cost_basis="no_charge", detail="本地 PostgreSQL 查询，无独立 API 费"):
        rows = db.query(f"""
        SELECT news_id id, title, left(body, 240) excerpt, source, url, published_at, ingested_at
        FROM {db.qualified("news_item")} WHERE published_at BETWEEN %s AND %s AND (%s OR ingested_at <= %s)
          AND (NOT %s OR source<>'chinanews-finance' OR published_at + interval '1 minute' <= %s)
        ORDER BY published_at DESC, news_id DESC LIMIT 500
        """, [as_of - timedelta(days=3), as_of, replay, as_of, replay, as_of])
    result = []
    for row in rows:
        observed = row.pop("ingested_at")
        result.append({**row, "published_at": row["published_at"].isoformat(),
                       **({"received_at": observed.isoformat()} if not replay else {})})
    return result


def _memory(as_of):
    rows = db.query(
        f"""
        SELECT board_id, board, proxy_code, horizon_days, count(hit)::int samples,
               avg(hit::int)::float hit_rate, avg(realized_return)::float mean_return,
               avg(power(prob_up - (realized_return > 0)::int, 2))::float brier
        FROM {db.qualified("agent_board_forecast")}
        WHERE scored_at <= %s AND target_date BETWEEN %s AND %s
        GROUP BY board_id, board, proxy_code, horizon_days HAVING count(hit) > 0
        ORDER BY samples DESC, board, horizon_days
    """,
        [as_of, as_of.date() - timedelta(days=180), as_of.date()],
    )
    for r in rows:
        r["lesson"] = (
            f"已验证 {r['samples']} 条，样本仍少，维持基础风险预算。"
            if r["samples"] < 8
            else "近期方向命中率低于 45%，下调该板块的配置权重。"
            if r["hit_rate"] < 0.45
            else "已验证表现未触发降权，维持基础风险预算。"
        )
    return rows


def _intraday(as_of, session_day, *, include_flow=False):
    if session_day != as_of.date() or not (time(9, 30) <= as_of.time() <= time(15)):
        return {}
    if include_flow:
        from . import board_spots

        observations = {}
        for bid, r in board_spots.available(as_of).items():
            reading = {
                "at": r["source_at"].isoformat(),
                "captured_at": r["captured_at"].isoformat(),
                "return": round(float(r["pct_chg"]) / 100, 6),
                "source": "同花顺板块分时" if r["source"] == "ths" else "东方财富板块行情",
                "source_code": r["source_code"],
                "catalog_code": r["catalog_code"],
            }
            if r["source"] == "eastmoney" and r["main_net"] is not None:
                reading["capital_flow"] = {
                    "net_yuan": float(r["main_net"]),
                    "net_pct_points": float(r["main_ratio"]) if r["main_ratio"] is not None else None,
                    "method": "盘中主力净流入估算，日内累计",
                    "source": "东方财富板块行情",
                }
            observations[bid] = reading
        return observations
    # The historical replay uses its archived, name-keyed observations as originally published.
    rows = db.query(
        f"""
        SELECT DISTINCT ON (board_name) board_name, ts, pct_chg
        FROM {db.qualified("board_intraday")}
        WHERE ts BETWEEN %s AND %s AND ts >= %s ORDER BY board_name, ts DESC
    """,
        [as_of - timedelta(minutes=30), as_of, datetime.combine(session_day, time(9, 30), SH)],
    )
    return {
        r["board_name"]: {
            "at": r["ts"].isoformat(),
            "return": round(float(r["pct_chg"]) / 100, 6),
        }
        for r in rows
        if r["pct_chg"] is not None and pd.notna(r["pct_chg"])
    }


def _account_context(as_of, day, replay=False):
    balance = None
    if not replay:
        balance = db.query_one(
            f"""SELECT v.trade_date,coalesce(v.cash,p.initial_capital)::float cash,
                       coalesce(v.total_value,p.initial_capital)::float total_value,
                       v.drawdown::float drawdown
                FROM {db.qualified("pod")} p LEFT JOIN LATERAL (
                    SELECT n.* FROM {db.qualified("pod_nav")} n WHERE n.pod_id=p.pod_id
                    AND n.trade_date<=%s AND NOT EXISTS (SELECT 1 FROM {db.qualified("pod_decision")} d
                        WHERE d.pod_id=n.pod_id AND d.trade_date=n.trade_date
                        AND (d.executed_at IS NULL OR d.executed_at>%s))
                    ORDER BY n.trade_date DESC LIMIT 1) v ON true
                WHERE p.strategy_key='agent_manager' AND p.status='active' LIMIT 1""", [day, as_of],
        )
        if balance:
            balance["trade_date"] = str(balance["trade_date"]) if balance["trade_date"] else None
    rows = db.query(
        f"""
        SELECT i.code, i.name, x.weight::float weight, x.trade_date
        FROM {db.qualified("pod_position")} x JOIN {db.qualified("instrument")} i USING(instrument_id)
        JOIN {db.qualified("pod")} p USING(pod_id) WHERE p.strategy_key='agent_manager'
        AND p.status='active' AND x.trade_date=(SELECT max(v.trade_date) FROM {db.qualified("pod_nav")} v
            WHERE v.pod_id=p.pod_id AND v.trade_date <= %s AND (%s OR NOT EXISTS (
                SELECT 1 FROM {db.qualified("pod_decision")} d WHERE d.pod_id=v.pod_id
                AND d.trade_date=v.trade_date AND (d.executed_at IS NULL OR d.executed_at>%s))))
        ORDER BY x.weight DESC LIMIT 16
    """,
        [day, replay, as_of],
    )
    trades = db.query(
        f"""
        SELECT o.execute_on, i.code, i.name, o.side, o.amount::float amount, o.fee::float fee
        FROM {db.qualified("pod_order")} o JOIN {db.qualified("pod")} p USING(pod_id)
        JOIN {db.qualified("instrument")} i USING(instrument_id)
        WHERE p.strategy_key='agent_manager' AND (%s OR o.created_at <= %s) AND o.execute_on <= %s
        ORDER BY o.execute_on DESC, o.order_id DESC LIMIT 8
    """,
        [replay, as_of, day],
    )
    if replay:
        previous = db.query(
            f"""SELECT as_of,payload->>'summary' summary FROM {db.qualified("agent_research")}
                WHERE as_of<%s AND mode IN ('llm','rules') ORDER BY as_of DESC,report_id DESC LIMIT 2""",
            [as_of],
        )
    else:
        midnight = datetime.combine(as_of.date(), time.min, SH)
        previous = db.query(
            f"""WITH prior AS (
                SELECT report_id,as_of,mode,payload,as_of >= %s same_day,
                       row_number() OVER (PARTITION BY as_of >= %s ORDER BY as_of DESC,report_id DESC) recency
                FROM {db.qualified("agent_research")} WHERE as_of<%s AND payload->>'run_mode'='live'
                AND coalesce((payload->>'recorded_at')::timestamptz,as_of)<=%s
            ) SELECT report_id,as_of,mode,payload->>'summary' summary,payload->>'stance' stance,
                     payload->'targets' targets,payload->'risks' risks,
                     (SELECT jsonb_agg(jsonb_build_object('board',b->>'board','intent',b->>'intent','reason',b->>'reason'))
                      FROM jsonb_array_elements(coalesce(payload->'watchlist','[]'::jsonb)) b) board_views
              FROM prior
              WHERE same_day OR recency<=12 ORDER BY as_of DESC,report_id DESC""",
            [midnight, midnight, as_of, as_of],
        )
    return {
        **({"balance": balance} if not replay else {}),
        "holdings": [{**r, "trade_date": str(r["trade_date"])} for r in rows],
        "recent_orders": [{**r, "execute_on": str(r["execute_on"])} for r in trades],
        "prior_research": [{**r, "as_of": r["as_of"].isoformat()} for r in previous],
    }


def _market(closed, as_of, news, memories, replay=False):
    """Prefer the sector index. If delayed, explicitly identify the mapped fund proxy."""
    if not closed:
        return [], {}, None, None
    dates = closed[-220:]
    first, last = dates[0], dates[-1]
    quotes = db.query_df(
        f"""
        SELECT b.board_id, b.name, q.trade_date, q.close::float price, q.source,
               q.amount::float amount, f.main_net::float flow
        FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
        LEFT JOIN {db.qualified("board_fund_flow")} f ON f.board_id=q.board_id AND f.trade_date=q.trade_date
            AND f.period='1d' AND (%s OR (f.ingested_at <= %s AND f.source=ANY(%s)))
        WHERE b.is_active AND q.trade_date BETWEEN %s AND %s AND (%s OR (q.ingested_at <= %s AND b.created_at<=%s)) AND q.close > 0
    """,
        [replay, as_of, list(market.VERIFIED_FLOWS), first, last, replay, as_of, as_of],
    )
    funds = db.query_df(
        f"""
        SELECT b.board_id, b.name, i.code, i.name fund, ib.source mapping_source,
               n.trade_date, n.nav::float price, n.source
        FROM {db.qualified("instrument_board")} ib JOIN {db.qualified("board")} b USING(board_id)
        JOIN {db.qualified("instrument")} i USING(instrument_id)
        LEFT JOIN {db.qualified("instrument_nav")} n ON n.instrument_id=i.instrument_id
          AND n.trade_date BETWEEN %s AND %s AND (%s OR n.ingested_at <= %s) AND n.nav > 0
        WHERE b.is_active AND i.is_active AND i.kind='otc'
          AND (%s OR (ib.updated_at<=%s AND b.created_at<=%s AND i.updated_at<=%s))
          AND (i.listed_on IS NULL OR i.listed_on <= %s)
          AND (ib.source <> 'name-match' OR
               (right(upper(trim(i.name)),1)='C' AND upper(i.name) ~ '(指数|联接|ETF|LOF)'
                AND strpos(i.name,b.name)>0 AND b.name NOT IN ('综合','其他','其它')))
        ORDER BY b.board_id, i.code, n.trade_date
    """,
        [first, last, replay, as_of, replay, as_of, as_of, as_of, last],
    )
    mapping_counts = funds.groupby("board_id")["code"].nunique().to_dict()
    funds = funds.dropna(subset=["trade_date"])
    latest_nav = max(funds["trade_date"], default=None)
    latest = max(
        [v for v in (max(quotes["trade_date"], default=None), latest_nav) if v], default=None
    )
    if latest is None:
        return [], {}, None, latest_nav
    current_flows = {}
    if not replay:
        current_flows = {
            r["board_id"]: market.flow_evidence(
                r["source"],
                r["kind"],
                r["trade_date"],
                r["net"],
                r["pct"],
                last,
                r["net_5d"],
                r["pct_5d"],
            )
            for r in db.query(
                f"""SELECT f.board_id,b.kind,f.source,f.trade_date,f.main_net::float net,
                           f.main_net_pct::float pct,w.main_net::float net_5d,w.main_net_pct::float pct_5d
                    FROM {db.qualified("board_fund_flow")} f JOIN {db.qualified("board")} b USING(board_id)
                    LEFT JOIN {db.qualified("board_fund_flow")} w ON w.board_id=f.board_id
                        AND w.trade_date=f.trade_date AND w.source=f.source AND w.period='5d' AND w.ingested_at<=%s
                    WHERE f.trade_date=%s AND f.ingested_at<=%s AND f.period='1d'
                      AND f.source=ANY(%s) AND f.main_net IS NOT NULL""",
                [as_of, last, as_of, list(market.VERIFIED_FLOWS)],
            )
        }
    # A stale report can explain its last observable signals. It cannot issue a forecast or an order.
    calendar = [d for d in dates if d <= latest]
    grouped = {int(k): g for k, g in quotes.groupby("board_id")}
    mapped = {int(k): g for k, g in funds.groupby("board_id")}
    candidates, series = [], {}
    for bid in sorted(grouped.keys() | mapped.keys()):
        g, fg = grouped.get(bid), mapped.get(bid)
        valid_funds, proxies = [], []
        if fg is not None:
            for code, fund in fg.groupby("code", sort=True):
                row = fund.iloc[-1]
                if row.trade_date != latest:
                    continue
                valid_funds.append(
                    {"code": str(code), "name": row.fund, "mapping_source": row.mapping_source}
                )
                proxies.append(fund)
        proxy = ""
        if g is None or g["trade_date"].max() != latest or len(g) < 25:
            if not proxies:
                continue
            g = max(proxies, key=len)
            proxy = str(g.iloc[-1].code)
        g = g.set_index("trade_date").sort_index()
        prices = g.price.reindex(calendar)
        if len(prices) < 25 or prices.count() < 25 or prices.iloc[[-1, -6, -21]].isna().any():
            continue
        name = g.iloc[-1]["name"]
        mom5 = float(prices.iloc[-1] / prices.iloc[-6] - 1)
        mom20 = float(prices.iloc[-1] / prices.iloc[-21] - 1)
        flow_ratio = None
        if not proxy:
            flow = g.reindex(calendar[-5:])[["flow", "amount"]].dropna()
            if len(flow) >= 3 and flow.amount.sum() > 0:
                flow_ratio = round(float(flow.flow.sum() / flow.amount.sum()), 6)
        matched = [
            n
            for n in news
            if len(name) > 1 and (name in n["title"] or name in (n["excerpt"] or ""))
        ][:3]
        record = {
            "board_id": bid,
            "board": name,
            "as_of": str(latest),
            "proxy_code": proxy,
            "source": str(g.iloc[-1].source),
            "momentum_5": round(mom5, 6),
            "momentum_20": round(mom20, 6),
            "flow_ratio": flow_ratio,
            "funds": valid_funds,
            "eligible_mappings": int(mapping_counts.get(bid, 0)),
            "fund_status": "可用"
            if valid_funds
            else "有基金映射，但同期净值缺失"
            if mapping_counts.get(bid)
            else "无合格基金映射",
            "news": matched,
            "memory_factor": 1.0,
        }
        relevant = [m for m in memories if m["board_id"] == bid and m["proxy_code"] == proxy]
        if any(m["samples"] >= 8 and m["hit_rate"] < 0.45 for m in relevant):
            record["memory_factor"] = 0.5
        record["evidence_ids"] = (
            [f"B{bid}"]
            + [f"N{n['id']}" for n in matched]
            + [f"M{bid}-{m['horizon_days']}" for m in relevant]
        )
        if not replay:
            record["capital_flow"] = current_flows.get(bid)
            if record["capital_flow"]:
                record["evidence_ids"].append(f"G{bid}")
        candidates.append(record)
        series[bid] = prices
    if candidates:
        frame = pd.DataFrame(candidates)
        # Direction and magnitude come from prices. Missing flow is neutral and news is context.
        flow = frame["flow_ratio"].astype(float).rank(pct=True).fillna(0.5)
        if not replay:
            # Rank the last verified session within the same provider's methodology.
            readings = pd.DataFrame(
                [r["capital_flow"] or {} for r in candidates], columns=["net_pct", "flow_source"]
            )
            flow = readings.groupby("flow_source")["net_pct"].rank(pct=True).fillna(0.5)
        ranks = (
            0.45 * frame.momentum_5.rank(pct=True)
            + 0.40 * frame.momentum_20.rank(pct=True)
            + 0.15 * flow
        )
        for r, rank in zip(candidates, ranks):
            r["score"] = round(float(rank) * 100, 2)
            source = f"映射基金 {r['proxy_code']} 净值" if r["proxy_code"] else "板块收盘价"
            r["reason"] = (
                f"按{source}，近 5 日 {r['momentum_5']:+.2%}，近 20 日 {r['momentum_20']:+.2%}。"
            )
        candidates.sort(key=lambda r: (-r["score"], r["board_id"]))
        candidates = list({r["board"]: r for r in reversed(candidates)}.values())
        candidates.sort(key=lambda r: (-r["score"], r["board_id"]))
    return candidates, series, latest, latest_nav


def _unique(rows):
    seen = set()
    result = []
    for row in rows:
        if row["board_id"] not in seen:
            result.append(row)
            seen.add(row["board_id"])
    return result


def _supported_percentages(text, rows, news):
    """Check quoted percentages against displayed data up to the stated rounding precision."""
    tokens = re.findall(r"([+\-−]?\d+(?:\.\d+)?)\s*[%％]", text)
    points = [0.0, 50.0, 100.0]
    for row in rows:
        values = [
            row.get(k)
            for k in ("momentum_5", "momentum_20", "flow_ratio", "forecast_factor", "memory_factor")
        ]
        values.append((row.get("intraday") or {}).get("return"))
        signal = row.get("opportunity") or {}
        metrics = signal.get("metrics") or {}
        values.extend(metrics.get(k) for k in ("close_location", "relative_5d", "analog_mean_5d",
                                              "analog_median_adverse", "analog_hit_rate"))
        values.extend((signal.get("memory") or {}).get(k) for k in
                      ("up_rate", "median_adverse_move", "median_favorable_move"))
        for f in row.get("outlooks", []):
            values.extend([f["prob_up"], f["expected_return"]])
            values.append(f.get("raw_prob_up"))
        for f in row.get("fund_features", {}).values():
            values.extend([f.get("daily_volatility"), f.get("round_trip_fee"), f.get("redemption_rate")])
        for f in row.get("fund_outlooks", []):
            values.extend([f.get("prob_up"), f.get("expected_return")])
        points.extend(100 * v for v in values if v is not None)
        # Daily flow percentages and the intraday feed already use percentage points.
        daily_flow = row.get("capital_flow") or {}
        spot_pct = ((row.get("intraday") or {}).get("capital_flow") or {}).get("net_pct_points")
        points.extend(
            v
            for v in (daily_flow.get("net_pct"), daily_flow.get("net_5d_pct"), spot_pct)
            if v is not None
        )
    for item in news:
        for v in re.findall(
            r"([+\-−]?\d+(?:\.\d+)?)\s*[%％]", item["title"] + (item["excerpt"] or "")
        ):
            points.append(float(v.replace("−", "-")))
    for token in tokens:
        value = float(token.replace("−", "-"))
        tolerance = 0.501 * 10 ** (-len(token.partition(".")[2]))
        # An unsigned value may follow a directional word such as “下跌”.
        signed = token[0] in "+-−"
        if not any(abs(value - (p if signed else abs(p))) <= tolerance for p in points):
            return False
    return True


def _selection(pool, response, global_news=(), *, replay=False, decision_day=None):
    if not response or response.get("stance") not in ("normal", "reduced", "cash"):
        return None
    watch = response.get("watch")
    if not isinstance(watch, list) or not 1 <= len(watch) <= 4:
        return None
    if not all(
        isinstance(response.get(k), str) and 0 < len(response[k].strip()) <= 600
        for k in ("thesis", "risk")
    ):
        return None
    source = {r["board_id"]: r for r in pool}
    global_refs = {f"N{n['id']}" for n in global_news}
    presented = response.get("_presented")
    if presented:
        global_refs.intersection_update(presented["macro_refs"])
        source = {
            k: {
                **v,
                "evidence_ids": [ref for ref in presented["boards"][k] if ref in v["evidence_ids"]],
            }
            for k, v in source.items()
            if k in presented["boards"]
        }
    all_refs = global_refs.union(*(r["evidence_ids"] for r in source.values()))
    thesis_refs = response.get("thesis_refs")
    if (
        not isinstance(thesis_refs, list)
        or not thesis_refs
        or any(not isinstance(v, str) or v not in all_refs for v in thesis_refs)
    ):
        return None
    selected = []
    for item in watch:
        if not isinstance(item, dict) or type(item.get("board_id")) is not int:
            return None
        row = source.get(item["board_id"])
        refs, reason = item.get("refs"), item.get("reason")
        if (
            not row
            or item.get("intent") not in ("allocate", "observe")
            or not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(x, str) or (x not in row["evidence_ids"] and x not in global_refs)
                for x in refs
            )
            or not isinstance(reason, str)
            or not 0 < len(reason.strip()) <= 400
            or (not replay and not behavior_policy.hypotheses_cited(reason, [row], refs))
            or not _supported_percentages(reason, [row], [*global_news, *row.get("news", [])])
            or (
                not replay
                and not flow_claims.supported(
                    reason,
                    pool,
                    refs,
                    subject=row,
                    decision_day=decision_day,
                )
            )
        ):
            return None
        market_reason = _market_text(reason, row.get("reason") or "持续跟踪该板块的量价变化。")
        intent = "observe" if response["stance"] == "cash" else item["intent"]
        selected.append({**row, "reason": market_reason, "refs": refs, "intent": intent})
    if len(_unique(selected)) != len(selected) or not _supported_percentages(
        response["thesis"] + response["risk"],
        pool,
        [*global_news, *(n for r in pool for n in r.get("news", []))],
    ):
        return None
    if not replay:
        if not behavior_policy.hypotheses_cited(
            response["thesis"] + "。" + response["risk"], selected, thesis_refs
        ):
            return None
        subject = selected[0] if len(selected) == 1 else None
        risk_refs = set(thesis_refs).union(*(r["refs"] for r in selected))
        if not flow_claims.supported(
            response["thesis"], pool, thesis_refs, subject=subject, decision_day=decision_day
        ) or not flow_claims.supported(
            response["risk"],
            pool,
            risk_refs,
            subject=subject,
            risk=True,
            decision_day=decision_day,
        ):
            return None
    names = "、".join(row.get("board", str(row["board_id"])) for row in selected)
    summary = _market_text(response["thesis"], f"本轮聚焦{names}的短中期趋势与预测分歧。")
    return selected, response["stance"], summary, response["risk"]


def _market_text(text, fallback):
    # Published actions and weights are rendered from intent and risk checks.
    # Keep the independent market clauses if a model also offered execution prose.
    action = r"配置|仓位|[加减调建清持空重轻满]仓|持币|现金|买入|卖出|申购|赎回|观望|观察|待机|参与|介入|持有|布局|暂缓|仅跟踪|只跟踪"
    clauses = [
        s.strip()
        for s in re.split(r"[，,。；;！!]+", text)
        if s.strip() and not re.search(action, s)
    ]
    return "，".join(clauses) + "。" if clauses else fallback


def _targets(watch, stance, *, has_investable_data=None):
    if has_investable_data is None:
        has_investable_data = any(r["funds"] for r in watch)
    if not has_investable_data:
        return None
    desired = [r for r in watch if r["intent"] == "allocate"]
    if stance == "cash" or not desired:
        return {}
    if not any(r["funds"] for r in desired):
        return None
    observe_codes = {f["code"] for r in watch if r["intent"] == "observe" for f in r["funds"]}
    strong = []
    for row in desired:
        funds = [f for f in row["funds"] if f["code"] not in observe_codes]
        if (
            funds
            and row["momentum_5"] > 0
            and row["momentum_20"] > 0
            and row.get("forecast_factor", 1.0) > 0
        ):
            strong.append({**row, "funds": funds})
    if not strong:
        return {}
    rules = load_settings().agent
    per_sector = min((0.75 if stance == "normal" else 0.35) / len(strong), rules.max_board_exposure)
    targets = {}
    for r in strong:
        per_fund = per_sector * r["memory_factor"] * r.get("forecast_factor", 1.0) / len(r["funds"])
        if per_fund <= 0:
            continue
        for fund in r["funds"]:
            code = fund["code"]
            targets[code] = round(min(rules.max_position, targets.get(code, 0.0) + per_fund), 6)
    return targets


def _allocation_summary(targets, watch):
    if targets is None:
        return "本轮未生成新目标，保留现有组合。"
    fund_names = {f["code"]: f.get("name") or f["code"] for r in watch for f in r["funds"]}
    parts = [f"{fund_names[code]}（{code}）{weight:.2%}" for code, weight in targets.items()]
    parts.append(f"现金 {1 - sum(targets.values()):.2%}")
    text = "本轮模拟目标为" + "、".join(parts) + "。"
    observing = [r["board"] for r in watch if r["intent"] == "observe"]
    if observing:
        text += "仅观察" + "、".join(observing) + "，本轮不为这些板块配置基金。"
    return text


def _forecast(prices, horizon):
    """Non-overlapping historical outcomes conditioned on the sign of five-day momentum."""
    trend = prices / prices.shift(5) - 1.0
    forward = prices.shift(-horizon) / prices - 1.0
    matched = []
    # Anchor backwards so overlapping multi-day returns don't count as independent observations.
    for i in range(len(prices) - horizon - 1, 4, -horizon):
        if (
            pd.notna(trend.iloc[i])
            and pd.notna(forward.iloc[i])
            and (trend.iloc[i] > 0) == (trend.iloc[-1] > 0)
        ):
            matched.append(float(forward.iloc[i]))
    if len(matched) < 8:
        return None
    n = len(matched)
    prob = (sum(x > 0 for x in matched) + 5) / (n + 10)
    return {
        "prob_up": round(prob, 4),
        "expected_return": round(sum(matched) / (n + 10), 6),
        "samples": n,
        "confidence": "mid" if n >= 30 and abs(prob - 0.5) >= 0.07 else "low",
    }


def _candidate_outlooks(
    pool, history, base, due, as_of, strategy_version="v1", calibration=None, *, use_behavior=False
):
    locked = db.query(
        f"""
        SELECT board_id,proxy_code,horizon_days,prob_up,expected_return,confidence,samples,issued_at
        FROM {db.qualified("agent_board_forecast")} WHERE base_date=%s AND issued_at <= %s
    """,
        [base, as_of],
    )
    issued = {(r["board_id"], r["horizon_days"]): r for r in locked}
    samples = (
        behavior_policy.observations([r["board_id"] for r in pool], as_of) if use_behavior else {}
    )
    first_issue = {}
    if use_behavior and locked:
        metadata = db.query(
            f"""SELECT f.board_id,f.horizon_days,o.version,o.regime,o.base_prob_up,o.feedback
                FROM {db.qualified("agent_behavior_observation")} o JOIN {db.qualified("agent_board_forecast")} f USING(forecast_id)
                WHERE f.base_date=%s AND f.issued_at<=%s""",
            [base, as_of],
        )
        first_issue = {(r["board_id"], r["horizon_days"]): r for r in metadata}
    for row in pool:
        outlooks = []
        for horizon, end in due.items():
            old = issued.get((row["board_id"], horizon))
            if old and old["proxy_code"] != row["proxy_code"]:
                continue
            estimate = old or (
                manager_v2.forecast(
                    history[row["board_id"]],
                    horizon,
                    calibration or {},
                    lead_sessions=int(use_behavior and str(base) != row.get("as_of")),
                )
                if strategy_version == "v2"
                else _forecast(history[row["board_id"]], horizon)
            )
            if estimate:
                estimate = dict(estimate)
                if "issued_at" in estimate:
                    if use_behavior:
                        estimate["issued_at"] = estimate["issued_at"].isoformat()
                    else:
                        estimate.pop("issued_at")
                if use_behavior:
                    if old:
                        estimate["behavior_locked"] = True
                        saved = first_issue.get((row["board_id"], horizon))
                        if saved:
                            estimate.update(
                                behavior_version=saved["version"],
                                behavior_regime=saved["regime"],
                                base_prob_up=saved["base_prob_up"],
                                behavior_feedback=saved["feedback"],
                                behavior_adjustment=round(
                                    estimate["prob_up"] - saved["base_prob_up"], 4
                                ),
                            )
                    else:
                        estimate["horizon_days"] = horizon
                        behavior_policy.apply_feedback(
                            row,
                            estimate,
                            samples.get((row["board_id"], row["behavior"]["version"]), []),
                            as_of,
                        )
                outlooks.append(
                    {
                        **estimate,
                        "horizon_days": horizon,
                        "base_date": str(base),
                        "target_date": str(end),
                    }
                )
        row["outlooks"] = outlooks
        row["forecast_factor"], row["forecast_note"] = _forecast_budget(outlooks)
        if outlooks:
            row["evidence_ids"].append(f"F{row['board_id']}")


def _forecast_budget(outlooks):
    negative = sum(r["prob_up"] < 0.5 and r["expected_return"] < 0 for r in outlooks)
    if negative == 2:
        return 0.0, "两个期限的历史上涨频率均低于五成且平均预期收益为负，本次只观察。"
    if negative:
        return 0.5, "一个期限的上涨频率低于五成且平均预期收益为负，该板块预算减半。"
    if any(r["prob_up"] < 0.5 or r["expected_return"] < 0 for r in outlooks):
        return 0.75, "历史上涨频率与平均收益的方向存在分歧，考虑涨跌幅度不对称，预算降至四分之三。"
    if len(outlooks) < 2:
        return 0.75, "部分期限样本不足，配置预算降至四分之三。"
    return 1.0, "历史频率未触发预算折减。"


def _persist(report, forecasts):
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {db.qualified('agent_research')}(as_of,data_as_of,mode,payload) "
            "VALUES (%s,%s,%s,%s) RETURNING report_id",
            [report["as_of"], report["data_as_of"], report["mode"], Jsonb(report)],
        )
        report_id = cur.fetchone()["report_id"]
        usage.attach_report(conn, report_id)
        for f in forecasts:
            cur.execute(
                f"""
                INSERT INTO {db.qualified("agent_board_forecast")}
                  (issued_at,board_id,board,proxy_code,base_date,target_date,horizon_days,prob_up,expected_return,confidence,samples)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (board_id,base_date,horizon_days) DO NOTHING RETURNING forecast_id
                """,
                [
                    report["as_of"],
                    *(
                        f[k]
                        for k in (
                            "board_id",
                            "board",
                            "proxy_code",
                            "base_date",
                            "target_date",
                            "horizon_days",
                            "prob_up",
                            "expected_return",
                            "confidence",
                            "samples",
                        )
                    ),
                ],
            )
            inserted = cur.fetchone()
            if inserted and "behavior_version" in f:
                cur.execute(
                    f"""INSERT INTO {db.qualified("agent_behavior_observation")}
                        (forecast_id,version,regime,base_prob_up,feedback) VALUES (%s,%s,%s,%s,%s)""",
                    [
                        inserted["forecast_id"],
                        f["behavior_version"],
                        f["behavior_regime"],
                        f["base_prob_up"],
                        Jsonb(f["behavior_feedback"]),
                    ],
                )
            cur.execute(
                f"INSERT INTO {db.qualified('agent_report_forecast')} (report_id,forecast_id) "
                f"SELECT %s,forecast_id FROM {db.qualified('agent_board_forecast')} "
                "WHERE board_id=%s AND base_date=%s AND horizon_days=%s AND target_date=%s AND proxy_code=%s AND issued_at<=%s",
                [report_id, f["board_id"], f["base_date"], f["horizon_days"], f["target_date"], f["proxy_code"], report["as_of"]],
            )
        if "fund_forecasts" in report:
            fund_forecasts.persist(
                report_id, report["fund_forecasts"], _clock(datetime.fromisoformat(report["as_of"]))
            )
    return report_id


def generate_research(as_of: datetime, *, live: bool = True, use_model=True, freeze_forecasts=True) -> dict:
    with usage.scope("agent_research", inherit=True):
        return _generate(as_of, live=live, replay=False, use_model=use_model, strategy_version="v2",
                         freeze_forecasts=freeze_forecasts)


def _assert_replay_database():
    if not re.fullmatch(
        r"quantlab_replay_[a-z0-9_]+", db.query_one("SELECT current_database() AS name")["name"]
    ):
        raise RuntimeError("历史经理只允许在独立的 quantlab_replay_* 数据库中运行")
    accounts = db.query(
        f"SELECT entry_evidence->>'mode' AS mode FROM {db.qualified('pod')} WHERE strategy_key='agent_manager'"
    )
    if len(accounts) != 1 or accounts[0]["mode"] != "historical_replay":
        raise RuntimeError("独立回放库应含一个明确标记 historical_replay 的经理账户")


def generate_replay_research(
    as_of: datetime, *, use_model: bool = True, strategy_version: str = "v1"
) -> dict:
    """One immutable research decision per simulated day, using the live manager's policy."""
    as_of = _clock(as_of)
    if as_of.date() >= datetime.now(SH).date() or not time(9, 30) <= as_of.time() < time(15):
        raise ValueError("回放研究需要已经收盘的历史日期及交易时段内的决策时间")
    with db.transaction():
        _assert_replay_database()
        manager = db.query_one(
            f"SELECT entry_evidence FROM {db.qualified('pod')} WHERE strategy_key='agent_manager'"
        )
        locked_version = manager["entry_evidence"].get("strategy_version", "v1")
        if strategy_version not in ("v1", "v2") or locked_version != strategy_version:
            raise ValueError("回放策略版本与专用账户冻结的版本不一致")
        db.query_one("SELECT pg_advisory_xact_lock(hashtext(%s),794022004)", [db.schema()])
        existing = db.query_one(
            f"SELECT report_id, as_of, payload FROM {db.qualified('agent_research')} "
            "WHERE payload->>'run_mode'='historical_replay' AND as_of >= %s ORDER BY as_of LIMIT 1",
            [datetime.combine(as_of.date(), time.min, SH)],
        )
        if existing:
            if existing["as_of"] != as_of:
                raise ValueError("该回放序列已有同日其它时刻或更晚的研究，请按原决策时刻续跑")
            return {
                **existing["payload"],
                "id": existing["report_id"],
                "forecasts": _forecasts(existing["report_id"], as_of=as_of),
            }
        with usage.scope("historical_replay"):
            return _generate(
                as_of, live=False, replay=True, use_model=use_model, strategy_version=strategy_version
            )


def _generate(as_of, *, live, replay, use_model, strategy_version, freeze_forecasts=True):
    as_of = _clock(as_of)
    closed, base, due = _sessions(as_of, replay=replay)
    window = forecast_window(as_of, base, due)
    fresh_clock = abs((datetime.now(SH) - as_of).total_seconds()) < 900
    if replay and base != as_of.date():
        raise ValueError("回放决策日不在已载入的交易日历中")
    publish = replay or (live and fresh_clock)
    memories = _memory(as_of)
    news = _news(as_of, replay)
    candidates, history, latest, nav_date = _market(closed, as_of, news, memories, replay)
    snaps = _intraday(as_of, base, include_flow=not replay)
    for row in candidates:
        row["intraday"] = snaps.get(row["board"] if replay else row["board_id"])
        if row["intraday"]:
            row["evidence_ids"].append(f"I{row['board_id']}")
    available_day = closed[-1] if closed else None
    account = _account_context(as_of, available_day or as_of.date(), replay)
    fresh = bool(
        candidates
        and latest == available_day
        and due
        and available_day
        and (as_of.date() - available_day).days <= 15
    )
    tradable = [r for r in candidates if r["funds"]]
    use_behavior = live and publish and fresh and not replay
    held = {f["code"]: f["weight"] for f in account["holdings"]}
    screen = None
    application_days = [base]
    if use_behavior:
        behavior_policy.annotate(candidates, closed, as_of, base)
        application_days = otc.next_application_sessions(as_of)
        screen = opportunity_research.scan(candidates, closed, as_of, application_days, held)
    pool = _unique(candidates[:5] + tradable[:5] + candidates[-2:])
    if screen:
        leaders = {s["board_id"] for s in screen["signals"][:3]}
        if screen["candidate"]:
            leaders.add(screen["candidate"]["board_id"])
        pool = _unique(pool + [r for r in candidates if r["board_id"] in leaders])
    if strategy_version == "v2":
        incumbents = []
        for c in candidates:
            c["held_codes"] = [f["code"] for f in c["funds"] if f["code"] in held]
            if c["held_codes"]:
                incumbents.append(c)
        pool = _unique(pool + incumbents)
    tools_used = {"status": "本轮没有发起在线工具调查", "calls": []}
    if live and fresh and publish and use_model and not replay:
        requests, tools_used["status"] = llm.research_queries(candidates, account, news)
        tools_used["calls"] = research_tools.investigate(requests, candidates, memories, as_of)
        if requests and not tools_used["calls"]:
            tools_used["status"] = "模型请求的工具或板块无效"
        requested = {c["board_id"] for c in tools_used["calls"]}
        pool = _unique(pool + [c for c in candidates if c["board_id"] in requested])
    candidate_ids = {r["board_id"] for r in pool}
    considered_memories = [m for m in memories if m["board_id"] in candidate_ids]
    if replay:
        considered_memories = considered_memories[:20]
    if use_behavior:
        behavior_followup.attach(pool, as_of, base)
    features, calibration = {}, {}
    fund_estimates = []
    method = manager_v2.METHOD if strategy_version == "v2" else FORECAST_METHOD
    if fresh:
        if strategy_version == "v2":
            calibration = manager_v2.calibration(as_of)
        _candidate_outlooks(
            pool,
            history,
            base,
            due,
            as_of,
            strategy_version,
            calibration,
            use_behavior=use_behavior,
        )
        if strategy_version == "v2":
            codes = sorted({f["code"] for r in pool for f in r["funds"]})
            application = application_days[0] if application_days else None
            fees = pod_store.fee_book(codes)
            features = manager_v2.fund_inputs(
                pool, history, fees, application, due.get(5) if application else None
            )
            if use_behavior:
                for code, feature in features.items():
                    feature["application_date"] = str(application) if application else None
                    feature["evaluation_date"] = str(due[5]) if 5 in due else None
                    if application and 5 in due:
                        days = (due[5] - application).days
                        feature["holding_days"] = days
                        feature["redemption_rate"] = holding_rate(
                            fees.get(code, {}).get("sell", []), days
                        )
            for row in pool:
                row["fund_features"] = {f["code"]: features[f["code"]] for f in row["funds"]}
        if use_behavior:
            fund_estimates = fund_forecasts.prepare(as_of, closed, base, due, pool, {}, account)
            for row in pool:
                codes = {f["code"] for f in row["funds"]}
                row["fund_outlooks"] = [f for f in fund_estimates if f["code"] in codes]
                row["evidence_ids"].extend(
                    f"U{code}"
                    for code in sorted(
                        {f["code"] for f in row["fund_outlooks"] if f.get("prob_up") is not None}
                    )
                )
    notes, forecasts = [], []
    if not snaps:
        notes.append("本次研究未使用当日盘中快照，短中期动量来自最近可用收盘数据。")
    if not replay and base == as_of.date() and time(9, 30) <= as_of.time() < time(15):
        observed = [r["intraday"] for r in candidates if r["intraday"]]
        notes.append(
            f"截至研究时点，{len(candidates)} 个候选板块中 {len(observed)} 个有同目录、同代码映射且在30分钟内的源站分时价，"
            f"{sum(bool(s.get('capital_flow')) for s in observed)} 个有可核验盘中净额。其余板块沿用已核验的收盘资料。"
        )
    if not fresh:
        notes.append("板块行情或交易日历覆盖不足，本轮保留已有持仓并暂停发布预测。")
    if not news:
        notes.append("最近三天未检索到已入库新闻，缺少消息面交叉验证。")
    if not tradable:
        notes.append("当前行情样本缺少具有同期净值的场外基金映射。")
    if not due or len(due) < 2:
        notes.append("已入库交易日历不足以覆盖所有预测期限。")
    if live and not fresh_clock:
        notes.append("历史研究只作回看，不追补可计分预测和交易目标。")
    coverage = {
        "news_screened": len(news),
        "newest_news_published_at": news[0]["published_at"] if news else None,
        "latest_news_received_at": max((r["received_at"] for r in news if r.get("received_at")), default=None),
        "boards_screened": len(candidates),
        "boards_with_intraday_price": sum(bool(r["intraday"]) for r in candidates),
        "boards_with_intraday_flow": sum(bool((r["intraday"] or {}).get("capital_flow")) for r in candidates),
        "latest_spot_captured_at": max((s["captured_at"] for s in snaps.values() if s.get("captured_at")), default=None),
        "boards_with_close_flow": sum(bool(r.get("capital_flow")) for r in candidates),
        "verified_sector_forecasts": sum(m["samples"] for m in memories),
        "prior_reports_read": [r["report_id"] for r in account["prior_research"] if "report_id" in r],
        "recent_confirmed_orders": len(account["recent_orders"]),
        "balance_date": (account.get("balance") or {}).get("trade_date"),
    }
    mode = "rules" if fresh else "insufficient"
    model, model_status = None, "本轮使用量化规则"
    summary_refs = []
    watch = [dict(r) for r in _unique(tradable[:2] + candidates[:2] + candidates[-1:])[:4]]
    for row in watch:
        row["refs"] = [f"B{row['board_id']}"]
        row["intent"] = (
            "allocate"
            if fresh
            and row["funds"]
            and row["momentum_5"] > 0
            and row["momentum_20"] > 0
            and row.get("forecast_factor", 1.0) > 0
            else "observe"
        )
    stance = (
        "normal"
        if sum(r["momentum_20"] > 0 for r in candidates) > len(candidates) / 2
        else "reduced"
    )
    summary = (
        "按板块短中期动量与资金强度筛选关注对象，观察趋势是否持续。"
        if fresh
        else "已核对数据库覆盖时间，当前数据不足以形成可执行的新策略。"
    )
    risks = ["趋势反转将削弱动量信号；已映射基金可能与板块走势偏离。"]
    if any(f["mapping_source"] == "name-match" for r in pool for f in r["funds"]):
        risks.append("名称匹配来自基金简称，未验证跟踪误差。标注代理代码的预测按该基金净值结算。")
    if fresh and publish and use_model:
        context = {
            "as_of": as_of.isoformat(),
            "research_date": str(latest),
            "candidates": pool,
            "forecast_method": method,
            "news": news[:6],
            "global_news_refs": [f"N{n['id']}" for n in news[:6]],
            "recent_experience": considered_memories,
            "paper_account": account,
            "historical_replay": replay,
            "strategy_version": strategy_version,
            "fund_features": features,
            **({"research_tools": tools_used, "forecast_window": window, "evidence_coverage": coverage} if not replay else {}),
        }
        response, model_status, model = llm.research_opinion(context)
        opinion = _selection(
            pool, response, news[:6], replay=replay, decision_day=str(as_of.date())
        )
        if (
            not opinion
            and not replay
            and (
                response is not None or model_status in (llm.LIVE_TRUNCATED, llm.LIVE_INVALID_JSON)
            )
        ):
            # The model gets the same decision clock and evidence for a single correction.
            # The first draft never enters portfolio planning or the forecast ledger.
            context["review_draft"] = True
            response, model_status, model = llm.research_opinion(context)
            opinion = _selection(pool, response, news[:6], decision_day=str(as_of.date()))
            if response is not None and not opinion:
                model_status = "复核生成的说明未通过证据校验，本轮使用量化规则"
        if opinion:
            mode, (watch, stance, summary, risk) = "llm", opinion
            summary_refs = response["thesis_refs"]
            risks.insert(0, risk)
        elif response is not None and "review_draft" not in context:
            model_status = "模型的板块选择、证据引用或百分比核对未通过校验，本轮使用量化规则"
    targets = (
        _targets(watch, stance, has_investable_data=bool(tradable)) if fresh and publish else None
    )
    if use_behavior:
        targets, fund_notes = behavior_policy.fund_entry_limit(
            targets, held, fund_estimates, features
        )
        notes.extend(fund_notes)
    if strategy_version == "v2":
        targets, entry_notes = manager_v2.entry_budget(targets, features, held)
        notes.extend(entry_notes)
    if use_behavior:
        targets = behavior_policy.limit_additions(watch, targets, held)
        notes.extend(
            r["behavior"]["impact"]["note"]
            for r in watch
            if r["behavior"]["impact"]["before_weight"] != r["behavior"]["impact"]["after_weight"]
        )
    allocation_summary = _allocation_summary(targets, watch)
    if targets is None:
        if not fresh:
            rationale = "行情历史或交易日历覆盖不足，维持已有组合。"
        elif not publish:
            rationale = "本次为历史或离线研究，保留已有组合。"
        else:
            rationale = "所选板块缺少合格且有同期净值的场外基金，保留已有组合。"
    elif not targets:
        rationale = (
            "本轮策略主动选择现金。"
            if stance == "cash" or all(r["intent"] == "observe" for r in watch)
            else "当前可投板块经过短中期趋势和多期限预测检验后，目标风险仓位归零。"
        )
    else:
        rationale = (
            "按所选板块分配风险预算，短、中期动量均为正才可申购；"
            f"单只基金占比不超过 {load_settings().agent.max_position:.0%}。目标投资比例 {sum(targets.values()):.1%}。"
        )
        if any(r["memory_factor"] < 1 for r in watch):
            rationale += "近期已验证方向命中率低的板块，本轮预算折半。"
    if targets is not None and stance != "cash":
        for row in watch:
            if (
                row["intent"] == "allocate"
                and row["funds"]
                and row["momentum_5"] > 0
                and row["momentum_20"] > 0
            ):
                rationale += f" {row['board']}：{row['forecast_note']}"
    if use_behavior and fund_notes:
        rationale += " " + " ".join(fund_notes)
    if publish and fresh:
        for row in watch:
            for forecast in row["outlooks"]:
                forecasts.append(
                    {
                        **forecast,
                        "board_id": row["board_id"],
                        "board": row["board"],
                        "proxy_code": row["proxy_code"],
                    }
                )
    if fresh and not forecasts:
        notes.append(
            "本轮未发布新预测；每个期限至少需要十二个已到期、互不重叠的历史样本。"
            if strategy_version == "v2"
            else "本轮未发布新预测；每个期限至少需要八个同方向、互不重叠的历史样本。"
        )
    report = {
        "run_mode": "historical_replay"
        if replay
        else "live"
        if live and fresh_clock
        else "offline",
        "as_of": as_of.isoformat(),
        "data_as_of": str(latest) if latest else None,
        "mode": mode,
        "model": model if mode == "llm" else "quantitative-rules",
        "model_status": model_status,
        "summary": summary,
        "summary_refs": summary_refs or [f"B{r['board_id']}" for r in watch],
        "rationale": allocation_summary
        + rationale
        + (
            " 盘中冻结指令还需复核前一日信号、先进先出批次费用和组合风险限制。"
            if strategy_version == "v2"
            else ""
        ),
        "strategy_version": strategy_version,
        "allocation_summary": allocation_summary,
        "risks": risks,
        "targets": targets,
        "stance": stance,
        "watchlist": watch,
        "memories": [m for m in memories if m["board_id"] in {r["board_id"] for r in watch}],
        "evidence": {
            **({"research_tools": tools_used} if not replay else {}),
            **({"replay_method": REPLAY_METHOD} if replay else {}),
            "candidates": pool,
            "considered_memories": considered_memories,
            "news": news[:12],
            "market_count": len(candidates),
            "nav_date": str(nav_date) if nav_date else None,
            "expected_data_date": str(available_day) if available_day else None,
            "notes": notes,
            "intraday_count": sum(bool(r["intraday"]) for r in candidates),
            "forecast_method": method,
            **(
                {"fund_features": features, "calibration": calibration}
                if strategy_version == "v2"
                else {}
            ),
            **account,
        },
    }
    if use_behavior:
        report["evidence"]["opportunities"] = screen
        report["fund_forecasts"] = fund_forecasts.selected_for_report(
            fund_estimates, watch, targets, account
        )
        report["evidence"]["behavior_screen"] = {
            str(r["board_id"]): r["behavior"]["regime"] for r in candidates
        }
    if not replay:
        report["forecast_window"] = window
        report["evidence"]["evidence_coverage"] = coverage
        report["recorded_at"] = datetime.now(SH).isoformat()
    if not freeze_forecasts:
        report["forecast_publication"] = "draft_estimates"
        if "fund_forecasts" in report:
            report["fund_estimates"] = report.pop("fund_forecasts")
    report["id"] = _persist(report, forecasts if freeze_forecasts else [])
    report["forecasts"] = _forecasts(report["id"], as_of=as_of)
    if use_behavior and freeze_forecasts:
        report["fund_forecasts"] = fund_forecasts.for_report(report["id"])
    return report


def settle_forecasts(as_of: datetime) -> int:
    return settle_forecast_counts(as_of)["boards"]


def settle_forecast_counts(as_of: datetime) -> dict:
    """Score the originally published instruments and expose each ledger's update count."""
    with db.transaction():
        return {"boards": _settle(as_of, replay=False), "funds": fund_forecasts.settle(as_of)}


def settle_replay_forecasts(as_of: datetime) -> int:
    _assert_replay_database()
    return _settle(as_of, replay=True)


def _settle(as_of, *, replay):
    as_of = _clock(as_of)
    closed, _, _ = _sessions(as_of, replay=replay)
    if not closed:
        return 0
    forecast = db.qualified("agent_board_forecast")
    return db.execute(
        f"""
        WITH prices AS (
            SELECT f.forecast_id, f.prob_up,
                COALESCE(b.close / NULLIF(a.close,0), n1.nav / NULLIF(n0.nav,0))::float - 1 AS ret
            FROM {forecast} f
            LEFT JOIN {db.qualified("board_quote")} a ON f.proxy_code='' AND a.board_id=f.board_id
                AND a.trade_date=f.base_date AND (%s OR a.ingested_at <= %s)
            LEFT JOIN {db.qualified("board_quote")} b ON f.proxy_code='' AND b.board_id=f.board_id
                AND b.trade_date=f.target_date AND (%s OR b.ingested_at <= %s)
            LEFT JOIN {db.qualified("instrument")} i ON i.code=f.proxy_code
            LEFT JOIN {db.qualified("instrument_nav")} n0 ON n0.instrument_id=i.instrument_id
                AND n0.trade_date=f.base_date AND (%s OR n0.ingested_at <= %s)
            LEFT JOIN {db.qualified("instrument_nav")} n1 ON n1.instrument_id=i.instrument_id
                AND n1.trade_date=f.target_date AND (%s OR n1.ingested_at <= %s)
            WHERE f.scored_at IS NULL AND f.target_date <= %s AND f.issued_at <= %s
              AND (NOT %s OR f.proxy_code='' OR
                   ((f.target_date+1) + time '08:30') AT TIME ZONE 'Asia/Shanghai' <= %s)
        )
        UPDATE {forecast} f SET realized_return=p.ret,
          hit=CASE WHEN p.prob_up <> .5 THEN ((p.prob_up > .5) = (p.ret > 0)) END, scored_at=%s
        FROM prices p WHERE p.forecast_id=f.forecast_id AND p.ret IS NOT NULL AND f.scored_at IS NULL
    """,
        [
            replay,
            as_of,
            replay,
            as_of,
            replay,
            as_of,
            replay,
            as_of,
            closed[-1],
            as_of,
            replay,
            as_of,
            as_of,
        ],
    )


def _forecasts(report_id=None, limit=150, *, as_of=None):
    join, params = "", []
    if report_id is not None:
        join = f"JOIN {db.qualified('agent_report_forecast')} r ON r.forecast_id=f.forecast_id AND r.report_id=%s"
        params.append(report_id)
    rows = db.query(
        f"""
        SELECT f.forecast_id id, f.board, f.proxy_code, f.issued_at, f.horizon_days, f.base_date, f.target_date,
               f.prob_up, f.expected_return, f.samples, f.confidence,
               CASE WHEN f.scored_at <= COALESCE(%s::timestamptz,'infinity') THEN f.realized_return END realized_return,
               CASE WHEN f.scored_at <= COALESCE(%s::timestamptz,'infinity') THEN f.hit END hit,
               CASE WHEN f.scored_at <= COALESCE(%s::timestamptz,'infinity') THEN 'scored' ELSE 'pending' END status
        FROM {db.qualified("agent_board_forecast")} f {join}
        ORDER BY f.issued_at DESC, f.board, f.horizon_days LIMIT %s
    """,
        [as_of, as_of, as_of, *params, limit],
    )
    return [
        {
            **r,
            "instrument": f"映射基金 {r['proxy_code']}" if r["proxy_code"] else "板块指数",
            "base_date": str(r["base_date"]),
            "target_date": str(r["target_date"]),
            "issued_at": r["issued_at"].isoformat(),
        }
        for r in rows
    ]


def dashboard_payload(report_id=None):
    reports = db.query(f"""
        SELECT report_id id, as_of, data_as_of, mode, payload->>'summary' summary
        FROM {db.qualified("agent_research")} ORDER BY as_of DESC,report_id DESC LIMIT 50
    """)
    report_id = report_id if report_id is not None else (reports[0]["id"] if reports else None)
    row = (
        db.query_one(
            f"SELECT payload FROM {db.qualified('agent_research')} WHERE report_id=%s", [report_id]
        )
        if report_id
        else None
    )
    report = (
        {**row["payload"], "id": report_id, "forecasts": _forecasts(report_id)} if row else None
    )
    if report and "fund_forecasts" in report:
        report["fund_forecasts"] = fund_forecasts.for_report(report_id)
    return {
        "report": report,
        "reports": [
            {
                **r,
                "as_of": r["as_of"].isoformat(),
                "data_as_of": str(r["data_as_of"]) if r["data_as_of"] else None,
            }
            for r in reports
        ],
        "forecasts": _forecasts(),
        "memories": _memory(datetime.now(SH)),
    }
