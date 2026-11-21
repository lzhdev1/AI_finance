"""数据查询层：前端要的一切都在这里算好，API 只负责序列化。

约定：
* 所有函数返回纯 Python 结构（dict/list），不返回 DataFrame；
* 日期转 ISO 字符串，NaN 转 None——前端不用猜"这个 null 是什么"；
* 这里只读，不做任何写入；决策与结算是 runner 的事。

放在这里而不是写在 API 路由里，是为了让"口径"只有一份：
页面显示、回测、以后的 CLI 都走同一套。
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pandas as pd

from quantlab import db, intraday as session_metrics, learning

INDEX_ORDER = ["000001", "399001", "399006", "000300"]
INDEX_NAMES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
}


# ---------------------------------------------------------------- 序列化

def clean(value):
    """把 date / Decimal / NaN / numpy 标量转成能 JSON 序列化的值。"""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy 标量
        try:
            return clean(value.item())
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def clean_rows(rows) -> list[dict]:
    return [{key: clean(value) for key, value in dict(row).items()} for row in rows]


# ---------------------------------------------------------------- 顶部行情

def index_ticker() -> dict:
    """三级兜底：今天盘中 → 最近交易日的快照 → 日线表。"""

    def pack(rows, stamp: str) -> dict:
        ordered = sorted(
            rows, key=lambda row: INDEX_ORDER.index(row["index_code"]) if row["index_code"] in INDEX_ORDER else 99
        )
        return {
            "stamp": stamp,
            "items": [
                {
                    "code": row["index_code"],
                    "name": INDEX_NAMES.get(row["index_code"], row["index_code"]),
                    "pct": clean(row["pct_chg"]),
                    "price": clean(row["price"]),
                    "as_of": clean(row["ts"]),
                }
                for row in ordered
            ],
        }

    rows = db.query(f"""
        WITH quotes AS (
            (SELECT DISTINCT ON (index_code) index_code, pct_chg, price, ts, '盘中' kind
             FROM {db.qualified('index_intraday')} WHERE ts <= now() ORDER BY index_code, ts DESC)
            UNION ALL
            (SELECT DISTINCT ON (index_code) index_code, pct_chg, close price,
                    (trade_date + time '15:00') AT TIME ZONE 'Asia/Shanghai' ts, '收盘' kind
             FROM {db.qualified('index_quote')} WHERE trade_date <= current_date
             ORDER BY index_code, trade_date DESC)
        )
        SELECT DISTINCT ON (index_code) * FROM quotes WHERE price IS NOT NULL
        ORDER BY index_code, ts DESC, kind DESC
    """)
    if not rows:
        return {"stamp": "指数行情暂缺", "items": []}
    last = max(rows, key=lambda r: r["ts"])
    return pack(rows, last["ts"].strftime("%Y-%m-%d %H:%M") + " " + last["kind"])


# ---------------------------------------------------------------- 仓位列表

PODS_SQL = f"""
WITH latest AS (
    SELECT DISTINCT ON (pod_id) * FROM {db.qualified('pod_nav')}
    ORDER BY pod_id, trade_date DESC
),
picks AS (
    SELECT pod_id, rank FROM {db.qualified('daily_pick')}
    WHERE trade_date = (SELECT max(trade_date) FROM {db.qualified('daily_pick')})
),
intra AS (
    SELECT DISTINCT ON (pod_id) pod_id, est_pct
    FROM {db.qualified('pod_intraday')} WHERE ts::date = current_date
    ORDER BY pod_id, ts DESC
)
SELECT p.pod_id, p.pod_no, p.name, p.status, p.opened_on, p.thesis,
       l.trade_date AS last_date, l.cum_return, l.daily_return, l.drawdown,
       l.excess_return, l.benchmark_return, COALESCE(pk.rank, 0) AS pick_rank, i.est_pct
FROM {db.qualified('pod')} p
LEFT JOIN latest l USING (pod_id)
LEFT JOIN picks pk USING (pod_id)
LEFT JOIN intra i USING (pod_id)
ORDER BY (p.status = 'active') DESC, p.pod_no
"""


def pods_overview() -> list[dict]:
    rows = db.query(PODS_SQL)
    items = []
    for row in rows:
        item = {key: clean(value) for key, value in dict(row).items()}
        item["est_pct"] = clean(item["est_pct"])
        items.append(item)
    return items


# ---------------------------------------------------------------- 单仓详情

def _holdings(pod_id: int) -> list[dict]:
    positions = db.query(
        f"""
        SELECT i.instrument_id, i.code, i.name, pos.weight, pos.market_value, pos.avg_cost,
               string_agg(DISTINCT b.name, '、') AS boards
        FROM {db.qualified('pod_position')} pos
        JOIN {db.qualified('instrument')} i USING (instrument_id)
        LEFT JOIN {db.qualified('instrument_board')} ib USING (instrument_id)
        LEFT JOIN {db.qualified('board')} b USING (board_id)
        WHERE pos.pod_id = %s
          AND pos.trade_date = (SELECT max(trade_date) FROM {db.qualified('pod_nav')} WHERE pod_id = %s)
        GROUP BY i.instrument_id, i.code, i.name, pos.weight, pos.market_value, pos.avg_cost
        ORDER BY pos.weight DESC
        """,
        [pod_id, pod_id],
    )
    if not positions:
        return []

    ids = [int(row["instrument_id"]) for row in positions]
    navs = db.query(
        f"""
        SELECT instrument_id, trade_date, nav FROM {db.qualified('instrument_nav')}
        WHERE instrument_id = ANY(%s) AND trade_date >= current_date - interval '430 days'
        ORDER BY instrument_id, trade_date
        """,
        [ids],
    )

    by_instrument: dict[int, list[tuple[dt.date, float]]] = {}
    for row in navs:
        by_instrument.setdefault(int(row["instrument_id"]), []).append(
            (row["trade_date"], float(row["nav"]))
        )

    live = _realtime_map(pod_id)
    items = []
    for row in positions:
        series = by_instrument.get(int(row["instrument_id"]), [])
        last_nav = series[-1][1] if series else None
        ret_1y = None
        streak = 0
        if series:
            cutoff = series[-1][0] - dt.timedelta(days=365)
            anchor = None
            for day, nav in series:
                if day <= cutoff:
                    anchor = nav
                else:
                    break
            if anchor:
                ret_1y = last_nav / anchor - 1
            direction, count = 0, 0
            for index in range(len(series) - 1, 0, -1):
                delta = series[index][1] - series[index - 1][1]
                if abs(delta) < 1e-12:
                    break
                step = 1 if delta > 0 else -1
                if direction == 0:
                    direction = step
                elif step != direction:
                    break
                count += 1
            streak = count * direction

        items.append(
            {
                "code": row["code"],
                "name": row["name"],
                "boards": row["boards"],
                "weight": clean(row["weight"]),
                "market_value": clean(row["market_value"]),
                "avg_cost": clean(row["avg_cost"]),
                "last_nav": last_nav,
                "holding_return": (last_nav / float(row["avg_cost"]) - 1)
                if (last_nav and row["avg_cost"])
                else None,
                "ret_1y": ret_1y,
                "streak": streak,
                "live_pct": live.get(str(row["code"])),
            }
        )
    return items


def _realtime_map(pod_id: int) -> dict[str, float]:
    import json

    row = db.query_one(
        f"SELECT detail FROM {db.qualified('pod_intraday')} "
        "WHERE pod_id = %s AND ts::date = current_date ORDER BY ts DESC LIMIT 1",
        [pod_id],
    )
    if not row or not row["detail"]:
        return {}
    detail = row["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    return {str(item.get("code")): item.get("pct") for item in detail if item.get("code")}


def _history(pod_id: int) -> list[dict]:
    rows = db.query(
        f"""
        SELECT trade_date, unit_nav, total_value, cash, market_value, daily_return, cum_return, benchmark_return
        FROM {db.qualified('pod_nav')} WHERE pod_id = %s ORDER BY trade_date
        """,
        [pod_id],
    )
    return clean_rows(rows)


def _intraday(pod_id: int) -> tuple[list[dict], dict | None]:
    series = db.query(
        f"SELECT ts, est_pct FROM {db.qualified('pod_intraday')} "
        "WHERE pod_id = %s AND ts::date = current_date ORDER BY ts",
        [pod_id],
    )
    forecast = db.query_one(
        f"SELECT * FROM {db.qualified('pod_forecast_30m')} "
        "WHERE pod_id = %s AND trade_date = current_date",
        [pod_id],
    )
    return clean_rows(series), (clean(dict(forecast)) if forecast else None)


def _latest_card(pod_id: int):
    return db.query_one(
        f"""
        SELECT d.*, p.pod_no, p.name FROM {db.qualified('pod_decision')} d
        JOIN {db.qualified('pod')} p USING (pod_id)
        WHERE d.pod_id = %s
        ORDER BY d.trade_date DESC LIMIT 1
        """,
        [pod_id],
    )


def _related_news(terms: tuple[str, ...], days: int = 3) -> list[dict]:
    patterns = [f"%{term}%" for term in terms if term]
    if not patterns:
        return []
    rows = db.query(
        f"""
        SELECT title, source, published_at FROM {db.qualified('news_item')}
        WHERE published_at >= current_date - (%s || ' days')::interval
          AND (title ILIKE ANY(%s) OR COALESCE(body, '') ILIKE ANY(%s))
        ORDER BY published_at DESC LIMIT 6
        """,
        [str(days), patterns, patterns],
    )
    return clean_rows(rows)


def _code_names(codes: tuple[str, ...]) -> dict[str, str]:
    if not codes:
        return {}
    rows = db.query(
        f"SELECT code, name FROM {db.qualified('instrument')} WHERE code = ANY(%s)",
        [list(codes)],
    )
    return {row["code"]: row["name"] for row in rows}


def pod_detail(pod_id: int) -> dict | None:
    import json

    pod = db.query_one(
        f"SELECT pod_id, pod_no, name, status, opened_on, thesis, invalidation_rules "
        f"FROM {db.qualified('pod')} WHERE pod_id = %s",
        [pod_id],
    )
    if not pod:
        return None

    nav = db.query_one(
        f"SELECT * FROM {db.qualified('pod_nav')} WHERE pod_id = %s "
        "ORDER BY trade_date DESC LIMIT 1",
        [pod_id],
    )
    intra = db.query_one(
        f"SELECT est_pct FROM {db.qualified('pod_intraday')} "
        "WHERE pod_id = %s AND ts::date = current_date ORDER BY ts DESC LIMIT 1",
        [pod_id],
    )
    pick = db.query_one(
        f"SELECT rank FROM {db.qualified('daily_pick')} WHERE pod_id = %s "
        "ORDER BY trade_date DESC LIMIT 1",
        [pod_id],
    )

    history = _history(pod_id)
    holdings = _holdings(pod_id)
    card = _latest_card(pod_id)
    series, forecast = _intraday(pod_id)

    evidence_news: list[dict] = []
    news_ok = True
    prediction = None
    evidence = None

    if card:
        payload = card["payload"] or {}
        payload = json.loads(payload) if isinstance(payload, str) else payload
        pending = payload.get("pending") or []
        names = _code_names(tuple(str(item.get("code")) for item in pending))
        advice = [
            {
                "side": "买入" if item.get("side") == "buy" else "卖出",
                "name": names.get(str(item.get("code")), str(item.get("code"))),
                "amount": clean(item.get("amount")),
                "weight": clean(item.get("weight")),
            }
            for item in pending
        ]

        terms = tuple(
            {
                term
                for row in holdings
                for term in str(row.get("boards") or "").split("、")
                if term
            }
        )
        evidence_news = _related_news(terms)
        news_ok = bool(evidence_news)

        outlook = str(card["tomorrow_outlook"] or "")
        import re

        outlook = re.sub(r"^次日上涨概率[^；;。]*[；;。]?\s*", "", outlook).strip()

        prediction = {
            "trade_date": clean(card["trade_date"]),
            "executed": card["executed_at"] is not None,
            "confidence": clean(card["confidence"]),
            "action": card["action"],
            "advice": advice,
            "outlook": outlook,
        }
        evidence = {
            "rationale": card["rationale"],
            "news": evidence_news,
            "news_ok": news_ok,
            "narrative": payload.get("narrative"),
            "narrative_model": payload.get("narrative_model"),
            "summary": card["summary"],
        }

    return {
        "summary": {
            **{key: clean(value) for key, value in dict(pod).items()},
            "est_pct": clean(intra["est_pct"]) if intra else None,
            "cum_return": clean(nav["cum_return"]) if nav else None,
            "daily_return": clean(nav["daily_return"]) if nav else None,
            "drawdown": clean(nav["drawdown"]) if nav else None,
            "excess_return": clean(nav["excess_return"]) if nav else None,
            "benchmark_return": clean(nav["benchmark_return"]) if nav else None,
            "last_date": clean(nav["trade_date"]) if nav else None,
            "pick_rank": clean(pick["rank"]) if pick else 0,
            "confirm_date": history[0]["trade_date"] if history else None,
        },
        "holdings": [{key: clean(value) for key, value in row.items()} for row in holdings],
        "evidence": evidence,
        "prediction": prediction,
        "intraday": {"series": series, "forecast": forecast},
        "history": history,
    }


# ---------------------------------------------------------------- 决策卡

def cards_payload(day: dt.date | None = None) -> dict:
    import json

    if day is None:
        last = db.query_one(f"SELECT max(trade_date) AS day FROM {db.qualified('pod_decision')}")
        day = last["day"] or dt.date.today()
    evidence = board_evidence(day)
    cards = []

    rows = db.query(
        f"""
        SELECT d.*, p.pod_no, p.name FROM {db.qualified('pod_decision')} d
        JOIN {db.qualified('pod')} p USING (pod_id)
        WHERE d.trade_date = %s
        ORDER BY p.pod_no
        """, [day]
    )
    for card in rows:
        payload = card["payload"] or {}
        payload = json.loads(payload) if isinstance(payload, str) else payload
        pending = payload.get("pending") or []
        names = _code_names(tuple(str(item.get("code")) for item in pending))
        cards.append(
            {
                "pod_id": clean(card["pod_id"]),
                "pod_no": clean(card["pod_no"]),
                "name": card["name"],
                "trade_date": clean(card["trade_date"]),
                "action": card["action"],
                "executed": card["executed_at"] is not None,
                "confidence": clean(card["confidence"]),
                "rationale": card["rationale"],
                "outlook": card["tomorrow_outlook"],
                "long_term": card["long_term_view"],
                "invalidation": card["invalidation_note"],
                "verdict": card["newcomer_verdict"],
                "narrative": payload.get("narrative"),
                "narrative_model": payload.get("narrative_model"),
                "pending": [
                    {
                        "side": "买入" if item.get("side") == "buy" else "卖出",
                        "code": item.get("code"),
                        "name": names.get(str(item.get("code")), str(item.get("code"))),
                        "amount": clean(item.get("amount")),
                        "weight": clean(item.get("weight")),
                    }
                    for item in pending
                ],
            }
        )

    return {"evidence": evidence, "cards": cards}


def board_evidence(day: dt.date | None = None) -> dict:
    day = day or dt.date.today()
    frame = session_metrics.pool(day, top=15)
    stamps = session_metrics.snapshot_times(day)
    if frame is None or frame.empty:
        return {"stamp": None, "snapshots": 0, "degraded": None, "boards": []}

    degraded = not frame["main_net"].notna().any()
    boards = []
    for _, row in frame.iterrows():
        boards.append(
            {
                "name": row["board_name"],
                "pct": clean(row["pct_chg"]),
                "volume_ratio": clean(row["volume_ratio"]),
                "amount_share": clean(row["amount_share"]),
                "main_ratio": clean(row["main_ratio"]),
                "breadth": clean(row["breadth"]),
                "rs": clean(row["rs"]),
                "confirm": int(row["confirm_count"]),
                "score": clean(row["score"]),
            }
        )
    return {
        "stamp": stamps[-1] if stamps else None,
        "snapshots": len(stamps),
        "degraded": degraded,
        "boards": boards,
    }


# ---------------------------------------------------------------- 预测与因子

def predictions_payload() -> dict:
    rows = db.query(
        f"""
        SELECT p.trade_date, po.pod_no, po.name, p.prob_up, p.confidence,
               p.realized_return, p.hit, p.brier
        FROM {db.qualified('pod_prediction')} p
        JOIN {db.qualified('pod')} po USING (pod_id)
        WHERE p.target = 'pod_nav' AND p.horizon_days = 1
        ORDER BY p.trade_date DESC, po.pod_no
        """
    )
    scored = [row for row in rows if row["hit"] is not None]
    hit_rate = sum(float(row["hit"]) for row in scored) / len(scored) if scored else None
    brier = (
        sum(float(row["brier"]) for row in scored) / len(scored)
        if scored and all(row["brier"] is not None for row in scored)
        else None
    )
    calibration = learning.calibration()
    return {
        "stats": {
            "scored": len(scored),
            "hit_rate": hit_rate,
            "brier": brier,
            "total": len(rows),
        },
        "rows": [
            {
                "date": clean(row["trade_date"]),
                "pod_no": clean(row["pod_no"]),
                "name": row["name"],
                "prob_up": clean(row["prob_up"]),
                "confidence": row["confidence"],
                "realized": clean(row["realized_return"]),
                "hit": clean(row["hit"]),
                "brier": clean(row["brier"]),
            }
            for row in rows
        ],
        "calibration": {
            "enabled": bool(calibration.get("enabled")),
            "reason": calibration.get("reason"),
            "bins": [
                {
                    "range": bin_["range"],
                    "predicted": clean(bin_["predicted"]),
                    "actual": clean(bin_["actual"]),
                    "samples": bin_["samples"],
                }
                for bin_ in calibration.get("bins", [])
            ],
            "brier_raw": clean(calibration.get("brier_raw")),
            "brier_calibrated": clean(calibration.get("brier_calibrated")),
        },
    }


def factors_payload() -> dict:
    report = learning.learning_report()
    freshness = db.query(
        f"""
        SELECT '基金净值' AS name, to_char(max(trade_date), 'MM-DD') AS latest FROM {db.qualified('instrument_nav')}
        UNION ALL SELECT '板块行情', to_char(max(trade_date), 'MM-DD') FROM {db.qualified('board_quote')}
        UNION ALL SELECT '板块资金流', to_char(max(trade_date), 'MM-DD') FROM {db.qualified('board_fund_flow')}
        UNION ALL SELECT '板块实时', to_char(max(ts), 'MM-DD HH24:MI') FROM {db.qualified('board_intraday')}
        UNION ALL SELECT '基准指数', to_char(max(trade_date), 'MM-DD') FROM {db.qualified('index_quote')}
        UNION ALL SELECT '新闻', to_char(max(published_at), 'MM-DD HH24:MI') FROM {db.qualified('news_item')}
        """
    )
    runs = db.query(
        f"""
        SELECT run_date, phase, status FROM {db.qualified('pipeline_run')}
        ORDER BY run_id DESC LIMIT 8
        """
    )
    return {
        "disclaimer": "此页显示旧策略仓的因子诊断。经理的板块预测与经验记录见研究工作台。",
        "factors": [
            {
                "name": item["factor"],
                "days": item["days"],
                "ic_mean": clean(item["ic_mean"]),
                "icir": clean(item["icir"]),
                "positive_rate": clean(item["positive_rate"]),
            }
            for item in report["factor_stats"]
        ],
        "weights": [
            {
                "name": name,
                "now": clean(report["weights_now"][name]),
                "suggested": clean(report["weights_suggested"].get(name, 0)),
            }
            for name in report["weights_now"]
        ],
        "ready_for_weight_change": bool(report["ready_for_weight_change"]),
        "freshness": clean_rows(freshness),
        "runs": clean_rows(runs),
    }


# ---------------------------------------------------------------- 总览

def overview_payload() -> dict:
    from quantlab import factors as factor_defs
    from quantlab.config import load_settings

    settings = load_settings()
    counts = db.query_one(
        f"""
        SELECT
          (SELECT count(*) FROM {db.qualified('pod')} WHERE status = 'active') AS pods,
          (SELECT count(*) FROM {db.qualified('pod_decision')}
             WHERE trade_date = (SELECT max(trade_date) FROM {db.qualified('pod_decision')})) AS cards,
          (SELECT count(*) FROM {db.qualified('pod_prediction')} WHERE hit IS NOT NULL) AS preds,
          (SELECT max(trade_date) FROM {db.qualified('pod_nav')}) AS data_day
        """
    )
    return {
        "ticker": index_ticker(),
        "counts": {
            "pods": clean(counts["pods"]),
            "cards": clean(counts["cards"]),
            "preds": clean(counts["preds"]),
            "factors": len(factor_defs.WEIGHTS),
        },
        "meta": {
            "data_day": clean(counts["data_day"]),
            "pod_capital": settings.agent.pod_capital,
            "max_pods": settings.agent.max_pods,
            "cutoff": settings.agent.decision_cutoff,
        },
    }
