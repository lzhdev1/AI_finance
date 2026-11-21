"""Read-only workbench views of an atomically published historical simulation."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from quantlab import db
from quantlab.config import CONFIG_DIR, DATA_DIR
from quantlab.replay_verification import list_verifications

ARCHIVE = DATA_DIR / "manager-replay" / "latest.json"


def write_snapshot(file, payload):
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".", suffix=".tmp")
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, file)
    finally:
        tmp.unlink(missing_ok=True)


@lru_cache(maxsize=4)
def _read(path: Path, version: tuple) -> dict:
    return json.loads(path.read_bytes())


def current(path=None):
    file = path or ARCHIVE
    try:
        info = file.stat()
    except FileNotFoundError:
        return {"summary": {"available": False, "status": "empty"}, "days": []}
    return _read(file, (info.st_mtime_ns, info.st_size, info.st_ino))


def version_path(version):
    if version not in ("v1", "v2"):
        raise ValueError("未知回放版本")
    path = ARCHIVE.with_name(f"{version}.json")
    return ARCHIVE if version == "v1" and not path.exists() else path


def summary(version="v1"):
    result = current(version_path(version))["summary"]
    period = result.get("period")
    if period:
        disclosures = json.loads(
            (CONFIG_DIR / "replay_fund_disclosures.json").read_text(encoding="utf-8")
        )
        if disclosures["period"] == [period["start"], period["end"]]:
            return {**result, "fund_disclosures": disclosures}
    return result


def days_page(action, offset, limit, version="v1", day=None):
    rows = current(version_path(version))["days"]
    if action != "all":
        rows = [row for row in rows if row["action"] == action]
    if day:
        rows = [row for row in rows if row["trade_date"] == str(day)]
    return {
        "items": rows[offset : offset + limit],
        "total": len(rows),
        "has_more": offset + limit < len(rows),
    }


def max_drawdown(levels):
    peak, worst = 1.0, 0.0
    for level in levels:
        peak = max(peak, level)
        worst = min(worst, level / peak - 1.0)
    return worst


def forecast_stats(forecasts, through):
    stats = []
    for horizon in (1, 5):
        issued = [f for f in forecasts if f["horizon_days"] == horizon]
        scored = [f for f in issued if f["actual_return"] is not None]
        directional = [f for f in scored if f["hit"] is not None]
        hits = sum(f["hit"] for f in directional)
        losses = [(f["up_probability"] - int(f["actual_return"] > 0)) ** 2 for f in scored]
        pending = [f for f in issued if f["actual_return"] is None]
        overdue = sum(f["target_date"] <= through for f in pending)
        stats.append(
            {
                "horizon_days": horizon,
                "total": len(issued),
                "scored": len(scored),
                "post_verified": sum(f.get("after_replay_verified", False) for f in scored),
                "directional": len(directional),
                "hits": hits,
                "neutral": len(scored) - len(directional),
                "pending": len(pending),
                "overdue": overdue,
                "not_due": len(pending) - overdue,
                "baseline_up_rate": sum(f["actual_return"] > 0 for f in directional)
                / len(directional)
                if directional
                else None,
                "hit_rate": hits / len(directional) if directional else None,
                "brier": sum(losses) / len(losses) if losses else None,
            }
        )
    return stats


def _day_record(row, forecasts):
    report, card = row["report"], row["card"]
    evidence, watch = report["evidence"], report["watchlist"]
    if report.get("strategy_version") == "v2":
        estimates = {
            (s["board"], f["horizon_days"]): f for s in watch for f in s.get("outlooks", [])
        }
        forecasts = [
            {
                **f,
                **{
                    name: v
                    for name, v in estimates.get((f["sector_name"], f["horizon_days"]), {}).items()
                    if name in ("all_samples", "raw_prob_up", "calibration")
                },
            }
            for f in forecasts
        ]
    targets = card.get("targets")
    previous_weights = {h["code"]: h["weight"] for h in evidence.get("holdings", [])}
    funds = {f["code"]: s for s in watch for f in s["funds"]}
    reasons = {s["board_id"]: s["reason"] for s in watch}
    refs = set(report.get("summary_refs", []))
    for sector in watch:
        refs.update(sector.get("refs") or sector.get("evidence_ids", []))
    sources = {}
    news = {}
    for n in evidence["news"]:
        news[n["id"]] = n
    for s in [*evidence["candidates"], *watch]:
        key = "B" + str(s["board_id"])
        if key in refs or s["board_id"] in reasons:
            sources[key] = {
                "id": key,
                "kind": "market",
                "title": f"{s['board']} · 5日 {s['momentum_5']:+.2%}，20日 {s['momentum_20']:+.2%}",
                "published_at": s["as_of"],
                "source": s["source"],
                "cited": key in refs,
            }
        forecast_ref = f"F{s['board_id']}"
        if forecast_ref in refs:
            values = [
                f"{f['horizon_days']}日：上涨概率 {f['prob_up']:.1%}，预期收益 {f['expected_return']:+.2%}"
                for f in s.get("outlooks", [])
            ]
            sources[forecast_ref] = {
                "id": forecast_ref,
                "kind": "forecast",
                "title": f"{s['board']} · " + "；".join(values),
                "published_at": report["as_of"],
                "source": "决策时的历史样本估计",
                "cited": True,
            }
        intraday_ref = f"I{s['board_id']}"
        if intraday_ref in refs and s.get("intraday"):
            snapshot = s["intraday"]
            sources[intraday_ref] = {
                "id": intraday_ref,
                "kind": "market",
                "title": f"{s['board']} · 盘中 {snapshot['return']:+.2%}",
                "published_at": snapshot["at"],
                "source": "盘中行情快照",
                "cited": True,
            }
        for n in s.get("news", []):
            news[n["id"]] = n
    for n in news.values():
        key = "N" + str(n["id"])
        if key in refs or n in evidence["news"]:
            sources[key] = {
                "id": key,
                "kind": "news",
                "title": n["title"],
                "published_at": n["published_at"],
                "source": n["source"],
                "url": n.get("url"),
                "cited": key in refs,
            }
    for m in evidence.get("considered_memories", []):
        key = f"M{m['board_id']}-{m['horizon_days']}"
        if key in refs:
            sources[key] = {
                "id": key,
                "kind": "memory",
                "title": f"{m['board']} · {m['horizon_days']}日 · {m['lesson']}",
                "source": "已验证预测",
                "cited": True,
            }

    def reason(code, side):
        if side == "sell" and "硬止损" in row["rationale"]:
            return row["rationale"]
        change = ""
        if targets is not None:
            change = f"此前确认仓位 {previous_weights.get(code, 0):.1%}，本轮目标 {targets.get(code, 0):.1%}。"
        if side == "sell" and targets is not None and not targets.get(code):
            return change + (
                "退出当日配置，按冻结份额赎回。"
                if code not in funds
                else f"本轮仅保留观察或现金。{funds[code]['reason']}"
            )
        if code in funds:
            return change + funds[code]["reason"]
        return change + row["rationale"]

    return {
        "trade_date": str(row["trade_date"]),
        "strategy_version": report.get("strategy_version", "v1"),
        "research_targets": card.get("research_targets"),
        "as_of": report["as_of"],
        "data_as_of": report["data_as_of"],
        "action": row["action"],
        "summary": report["summary"],
        "rationale": row["rationale"],
        "settlement_summary": row["summary"],
        "risks": report["risks"],
        "model": report["model"],
        "mode": report["mode"],
        "model_status": report["model_status"],
        "risk_checks": card.get("risk_checks", []),
        "evidence": sorted(sources.values(), key=lambda x: not x["cited"]),
        "watchlist": [
            {
                "sector_name": s["board"],
                "intent": s["intent"],
                "reason": s["reason"],
                "refs": s.get("refs", []),
                "momentum_5": s["momentum_5"],
                "momentum_20": s["momentum_20"],
            }
            for s in watch
        ],
        "targets": [
            {
                "ts_code": c,
                "target_weight": weight,
                "sector_name": funds.get(c, {}).get("board"),
                "reason": reason(c, "buy"),
            }
            for c, weight in (targets or {}).items()
        ],
        "target_cash": None if targets is None else 1 - sum(targets.values()),
        "instructions": [
            {
                "ts_code": x["code"],
                "action": x["side"],
                "reason": reason(x["code"], x["side"]),
                "amount": x["amount"],
                "shares": x.get("shares"),
                "est_nav": x["est_nav"],
            }
            for x in card.get("instructions", [])
        ],
        "orders": [
            {
                "ts_code": x["code"],
                "action": x["side"],
                "shares": x["shares"],
                "nav": x["nav"],
                "amount": x["amount"],
                "fee": x["fee"],
                "reason": reason(x["code"], x["side"]),
            }
            for x in card.get("fills", [])
        ],
        "forecasts": forecasts,
        "holdings_before": evidence.get("holdings", []),
        "memories": report.get("memories", []),
        "notes": evidence.get("notes", []),
        "cash": row["cash"],
        "total_value": row["total_value"],
        "unit_nav": row["unit_nav"],
        "cum_return": row["unit_nav"] - 1,
    }


def publish(pod, sessions, *, path=None):
    """Export only the isolated account. All forecasts retain their original issue date."""
    table = db.qualified
    rows = db.query(
        f"""
        SELECT d.trade_date, d.action, d.summary, d.rationale, d.payload AS card, r.report_id, r.payload AS report,
               n.cash::float cash, n.total_value::float total_value, n.unit_nav::float unit_nav, q.close::float close
        FROM {table("pod_decision")} d JOIN {table("agent_research")} r ON r.report_id=(d.payload->>'research_id')::bigint
        JOIN {table("pod_nav")} n USING(pod_id,trade_date)
        LEFT JOIN {table("index_quote")} q ON q.trade_date=d.trade_date AND q.index_code=%s
        WHERE d.pod_id=%s AND d.executed_at IS NOT NULL ORDER BY d.trade_date
    """,
        [pod["benchmark_code"], pod["pod_id"]],
    )
    anchor = db.query_one(
        f"""SELECT c.trade_date,q.close::float close FROM {table("trade_calendar")} c
        LEFT JOIN {table("index_quote")} q ON q.trade_date=c.trade_date AND q.index_code=%s
        WHERE c.trade_date<%s ORDER BY c.trade_date DESC LIMIT 1""",
        [pod["benchmark_code"], sessions[0]],
    )
    if not anchor or not anchor["close"] or any(not r["close"] for r in rows):
        raise ValueError("沪深300收盘价覆盖不完整，无法生成同期对照")
    forecasts = db.query(f"""SELECT f.forecast_id, l.report_id, f.board sector_name, f.proxy_code, f.horizon_days, f.target_date::text,
        f.base_date::text, f.expected_return, f.samples, f.prob_up up_probability, f.realized_return actual_return, f.hit,
        CASE WHEN f.scored_at IS NULL THEN 'pending' ELSE 'scored' END score_status
        FROM {table("agent_board_forecast")} f JOIN {table("agent_report_forecast")} l USING(forecast_id)
        ORDER BY l.report_id, f.board, f.horizon_days""")
    ready = len(rows) == len(sessions)
    supplementary = {r["forecast_id"]: r for r in list_verifications()} if ready else {}
    by_report = defaultdict(list)
    for f in forecasts:
        v = supplementary.get(f["forecast_id"])
        f["after_replay_verified"] = False
        if v and f["actual_return"] is None and f["target_date"] <= str(sessions[-1]):
            f.update(
                actual_return=float(v["realized_return"]),
                hit=v["hit"],
                score_status="scored",
                after_replay_verified=True,
                verification_source=v["source"],
                verification_url=v["source_url"],
                verified_at=v["verified_at"].isoformat(),
                base_close=float(v["base_close"]),
                target_close=float(v["target_close"]),
            )
        by_report[f["report_id"]].append(f)
    days = [_day_record(row, by_report[row["report_id"]]) for row in rows]
    history = [
        {
            "trade_date": str(r["trade_date"]),
            "unit_nav": r["unit_nav"],
            "cum_return": r["unit_nav"] - 1,
            "benchmark_return": r["close"] / anchor["close"] - 1,
        }
        for r in rows
    ]
    capital = float(pod["initial_capital"])
    last = history[-1] if history else {"cum_return": 0.0, "benchmark_return": 0.0}
    actions = Counter(d["action"] for d in days)
    real_news = {s["id"] for d in days for s in d["evidence"] if s["kind"] == "news"}
    archive_news = db.query_one(
        f"""SELECT count(*) AS items, count(DISTINCT (published_at AT TIME ZONE 'Asia/Shanghai')::date) AS days
        FROM {table("news_item")} WHERE (published_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s::date-3 AND %s""",
        [sessions[0], sessions[-1]],
    )
    summary = {
        "available": True,
        "status": "ready" if ready else "running",
        "id": pod["entry_evidence"]["run_id"],
        "period": {
            "start": str(sessions[0]),
            "end": str(sessions[-1]),
            "completed": len(days),
            "sessions": len(sessions),
        },
        "account": {
            "name": pod["name"],
            "initial_capital": capital,
            "total_value": rows[-1]["total_value"] if rows else capital,
            "cash": rows[-1]["cash"] if rows else capital,
            "cum_return": last["cum_return"],
            "max_drawdown": max_drawdown(r["unit_nav"] for r in history),
            "benchmark_return": last["benchmark_return"],
            "benchmark_anchor": str(anchor["trade_date"]),
            "benchmark_max_drawdown": max_drawdown(1 + r["benchmark_return"] for r in history),
            "excess_return": last["cum_return"] - last["benchmark_return"],
        },
        "history": history,
        "forecast_stats": forecast_stats(
            [f for d in days for f in d["forecasts"]], days[-1]["trade_date"] if days else ""
        ),
        "coverage": {
            "model_days": sum(d["mode"] == "llm" for d in days),
            "rule_days": sum(d["mode"] != "llm" for d in days),
            "news_items": len(real_news),
            "news_days": sum(any(s["kind"] == "news" for s in d["evidence"]) for d in days),
            "source_news_items": archive_news["items"],
            "source_news_days": archive_news["days"],
            "actions": dict(actions),
            "orders": sum(len(d["orders"]) for d in days),
        },
        "limitations": [
            "上海时间每个交易日 14:30 冻结策略。新闻使用中新网财经归档标题与原文链接，按原始发布时间筛选；当日收盘价及当日基金净值在下单后才能参与结算。",
            "历史日线与基金净值来自事后归档，缺少逐条首次披露时间及修订版本。基金净值按次日早间可见建模。",
            "使用现有场外基金映射和历史净值覆盖筛选，存在基金池幸存者偏差。申赎按项目费率及到账规则模拟。",
            "历史报道保留原始行业和事件，现有模型可能携带训练期知识。本结果属于回顾性模拟。",
            "1日和5日从决策当天收盘起算，到期按原板块或已标注的基金代理评分。方向按上涨与未上涨二分。50% 中性概率只计入 Brier。相邻日期的5日预测有重叠。",
            "沪深300采用价格指数，自期初前一交易日收盘归一。最大回撤取每日收盘资产相对历史峰值的最大跌幅。",
        ],
    }
    write_snapshot(path or ARCHIVE, {"summary": summary, "days": days[::-1]})
    return summary
