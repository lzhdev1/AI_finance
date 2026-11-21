"""研究工作台。手动研究由调度进程领取，网页刷新不会中断任务。"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, time
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from . import board_spots, db, ingest, llm, pod_store, queries, research
from .config import load_settings

log = logging.getLogger(__name__)


def now():
    return datetime.now(ZoneInfo(load_settings().timezone))


def scheduled_time(day, key):
    return datetime.combine(day, time.fromisoformat(load_settings().schedule[key]),
                            ZoneInfo(load_settings().timezone))


def ensure_manager(*, publication=False):
    with db.transaction() as conn:
        if publication:
            conn.execute("SET LOCAL lock_timeout='2s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
        db.query_one("SELECT pg_advisory_xact_lock(hashtext(%s), 794021002)", [db.schema()])
        manager = db.query_one(
            f"SELECT * FROM {db.qualified('pod')} WHERE strategy_key='agent_manager'"
        )
        if manager:
            return manager
        pod_id = pod_store.create_pod(
            name="基金经理模拟账户",
            strategy_key="agent_manager",
            thesis="按板块研究、基金映射和历史预测复盘配置模拟资金。",
            invalidation_rules=["过期或缺失的数据暂停新增交易", "风险约束优先于研究目标"],
            entry_evidence={"mode": "simulation"},
            trade_date=now().date(),
        )
        return pod_store.get_pod(pod_id)


def heartbeat():
    db.execute(f"""INSERT INTO {db.qualified("scheduler_state")} (service, heartbeat_at)
        VALUES ('scheduler', now()) ON CONFLICT (service) DO UPDATE SET heartbeat_at=now()""")


def scheduler_state():
    return db.query_one(f"""SELECT heartbeat_at AS last_heartbeat,
        heartbeat_at > now() - interval '90 seconds' AS scheduler_online
        FROM {db.qualified("scheduler_state")} WHERE service='scheduler'""") or {
        "last_heartbeat": None,
        "scheduler_online": False,
    }


def watch_evidence(stamp):
    """Queue research only for evidence that became visible since the last live report."""
    previous = db.query_one(
        f"""SELECT as_of FROM {db.qualified("agent_research")}
        WHERE as_of<=%s AND payload->>'run_mode'='live' ORDER BY as_of DESC LIMIT 1""",
        [stamp],
    )
    if previous is None:
        return "尚无在线研究，安排首次调查"
    last = previous["as_of"]
    flags = db.query_one(
        f"""SELECT EXISTS (SELECT 1 FROM {db.qualified("news_item")}
             WHERE published_at BETWEEN %s::timestamptz-interval '3 days' AND %s
               AND ingested_at<=%s AND greatest(ingested_at,published_at)>%s) AS news,
             EXISTS (SELECT 1 FROM {db.qualified("board_quote")} WHERE ingested_at>%s AND ingested_at<=%s
               AND trade_date BETWEEN %s::date-7 AND %s::date) AS prices,
             EXISTS (SELECT 1 FROM {db.qualified("board_fund_flow")} WHERE ingested_at>%s AND ingested_at<=%s
               AND trade_date BETWEEN %s::date-7 AND %s::date) AS flows""",
        [
            stamp,
            stamp,
            stamp,
            last,
            last,
            stamp,
            stamp.date(),
            stamp.date(),
            last,
            stamp,
            stamp.date(),
            stamp.date(),
        ],
    )
    flags["snapshot"] = any(
        row["captured_at"] > last for row in board_spots.available(stamp).values()
    )
    changed = [
        label
        for key, label in (
            ("news", "新闻"),
            ("prices", "行情"),
            ("flows", "资金流"),
            ("snapshot", "盘中快照"),
        )
        if flags[key]
    ]
    return f"{'、'.join(changed)}已有更新，安排经理研究" if changed else None


class ResearchBusy(Exception):
    pass


def queue_research(trigger="manual"):
    """串行检查与入队，避免双击或多个 API 实例重复调模型。"""
    with db.transaction():
        db.query_one("SELECT pg_advisory_xact_lock(hashtext(%s), 794021003)", [db.schema()])
        active = db.query_one(f"""SELECT run_id FROM {db.qualified("pipeline_run")}
            WHERE phase='agent_research' AND status='running' LIMIT 1""")
        if active:
            raise ResearchBusy(int(active["run_id"]))
        return db.query_one(
            f"""INSERT INTO {db.qualified("pipeline_run")}
            (run_date,phase,stats) VALUES (%s,'agent_research',%s) RETURNING run_id""",
            [now().date(), Jsonb({"stage": "queued", "trigger": trigger})],
        )


@contextmanager
def _research_worker_lock():
    # 专用 session lock 随进程退出释放。网络 I/O 期间不持有数据库事务。
    with psycopg.connect(db.dsn(), autocommit=True) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), 794021004)", [db.schema()]
        ).fetchone()[0]
        yield locked


def _research_run(run_id, trigger, refresh_news):
    run_table = db.qualified("pipeline_run")
    stats = {"trigger": trigger, "steps": []}

    def stage(name):
        stamp = now().isoformat()
        if stats["steps"]:
            stats["steps"][-1]["finished_at"] = stamp
        stats["steps"].append({"stage": name, "started_at": stamp})
        stats["stage"] = name
        db.execute(f"UPDATE {run_table} SET stats=%s WHERE run_id=%s", [Jsonb(stats), run_id])

    try:
        decision = trigger in {"decision", "decision_prepare"}
        if decision:
            from . import realtime
            stage("snapshot")
            snapshot = realtime.snapshot()
            stats["snapshot"] = {key: snapshot.get(key) for key in ("ts", "boards", "indexes")}
        if refresh_news:
            stage("news")
            stats["news"] = {}
            stats["inserted_news"] = ingest.update_news(audit=stats["news"])
        stage("review")
        counts = research.settle_forecast_counts(now())
        stats.update(settled_forecasts=counts["boards"], settled_fund_forecasts=counts["funds"])
        stage("analysis")
        if trigger == "decision_prepare":
            report = research.generate_research(now(), use_model=False, freeze_forecasts=False)
        elif trigger == "decision":
            with llm.research_deadline(scheduled_time(now().date(), "decision_retry")):
                report = research.generate_research(now())
        else:
            report = research.generate_research(now())
        stats["steps"][-1]["finished_at"] = now().isoformat()
        stats.update(
            stage="finished",
            report_id=report["id"],
            mode=report["mode"],
            summary=report["summary"],
            model_status=report["model_status"],
            watchlist=[row["board"] for row in report["watchlist"]],
            forecasts=len(report["forecasts"]),
            fund_forecasts=sum(f["status"] != "insufficient" for f in report.get("fund_forecasts", [])),
        )
        complete = (report["mode"] == "llm" or (trigger == "decision_prepare" and report["mode"] == "rules"))
        complete = complete and not stats.get("news", {}).get("warnings")
        db.execute(
            f"""UPDATE {run_table} SET status=%s, finished_at=now(), stats=%s
            WHERE run_id=%s""",
            ["success" if complete else "partial", Jsonb(stats), run_id],
        )
        if decision:
            report["decision_inputs"] = {"snapshot": stats["snapshot"], "news": stats["news"]}
        return report
    except Exception as exc:
        db.execute(
            f"""UPDATE {run_table} SET status='failed', finished_at=now(), stats=%s,
            error=%s WHERE run_id=%s""",
            [Jsonb(stats), f"{stats.get('stage')} · {type(exc).__name__}", run_id],
        )
        raise


def process_queue():
    from .runner import is_trade_day
    stamp = now()
    schedule = load_settings().schedule
    if (schedule["decision_prepare"] <= stamp.strftime("%H:%M") < schedule["decision_publish_by"]
            and stamp.weekday() < 5 and is_trade_day(stamp.date())):
        return
    with _research_worker_lock() as locked:
        if not locked:
            return
        runs = db.qualified("pipeline_run")
        # 已经领取的任务失去了 session lock，说明上一个 worker 意外退出。
        db.execute(f"""UPDATE {runs} SET status='failed', finished_at=now(), error='研究执行进程已中断'
            WHERE phase='agent_research' AND status='running' AND stats->>'stage' <> 'queued'""")
        row = db.query_one(f"""SELECT run_id, stats FROM {runs}
            WHERE phase='agent_research' AND status='running' AND stats->>'stage'='queued'
            ORDER BY run_id LIMIT 1""")
        if row:
            trigger = row["stats"]["trigger"]
            _research_run(row["run_id"], trigger, refresh_news=trigger != "watch")


def decision_research(*, prepare=False):
    # 交易时段必须重新取证。与手动任务互斥，未能生成新报告时不下单。
    with _research_worker_lock() as locked:
        if not locked:
            raise RuntimeError("已有研究正在运行，本次等待下一个决策窗口")
        trigger = "decision_prepare" if prepare else "decision"
        row = db.query_one(
            f"""INSERT INTO {db.qualified("pipeline_run")} (run_date,phase,stats)
            VALUES (%s,'agent_research',%s) RETURNING run_id""",
            [now().date(), Jsonb({"stage": "analysis", "trigger": trigger})],
        )
        return _research_run(row["run_id"], trigger, refresh_news=True)


def completed_decision_research(day, through):
    row = db.query_one(f"""SELECT r.report_id, r.payload, p.stats
        FROM {db.qualified('pipeline_run')} p
        JOIN {db.qualified('agent_research')} r ON r.report_id=(p.stats->>'report_id')::bigint
        WHERE p.run_date=%s AND p.phase='agent_research' AND p.status IN ('success','partial')
          AND p.stats->>'trigger' IN ('decision','decision_prepare')
          AND r.as_of BETWEEN %s AND %s AND r.payload->>'run_mode'='live'
        ORDER BY r.as_of DESC, r.report_id DESC LIMIT 1""",
        [day, scheduled_time(day, "decision_prepare"), through])
    if not row:
        return None
    stats = row["stats"]
    return {**row["payload"], "id": row["report_id"], "decision_inputs": {
        "snapshot": stats.get("snapshot", {}), "news": stats.get("news", {})},
        "decision_source": "prepared_review" if stats["trigger"] == "decision_prepare" else "final_review"}


def final_refresh_inputs(day):
    row = db.query_one(f"""SELECT stats FROM {db.qualified('pipeline_run')}
        WHERE run_date=%s AND phase='agent_research' AND stats->>'trigger'='decision'
          AND started_at >= %s ORDER BY run_id DESC LIMIT 1""",
        [day, scheduled_time(day, "intraday_decide")])
    if row and "news" in row["stats"] and row["stats"].get("snapshot"):
        return {"snapshot": row["stats"]["snapshot"], "news": row["stats"]["news"]}
    return None


def history(kind, offset=0, limit=40, horizon=None, status=None, *, public=False):
    if kind == "fund_forecasts":
        from . import fund_forecasts
        return fund_forecasts.history(offset=offset,limit=limit,horizon=horizon,status=status)
    runs = db.qualified("pipeline_run")
    params = []
    order_sql = f"""SELECT o.order_id,o.pod_id,p.name AS pod_name,i.code,i.name,o.side,
        o.amount,o.shares,o.nav,o.fee,o.reason,o.execute_on,o.decided_on FROM {db.qualified("pod_order")} o
        JOIN {db.qualified("instrument")} i USING (instrument_id) JOIN {db.qualified("pod")} p USING (pod_id)"""
    if kind == "orders":
        sql = order_sql + " ORDER BY o.execute_on DESC, o.order_id DESC"
    elif kind == "runs":
        if public:
            # Raw exception text and arbitrary task stats may contain connection details.
            sql = f"""SELECT run_id,run_date,phase,started_at,finished_at,status,
                jsonb_strip_nulls(jsonb_build_object('summary',stats->'summary','stage',stats->'stage',
                    'steps',stats->'steps','report_id',stats->'report_id','model_status',stats->'model_status')) AS stats
                FROM {runs} ORDER BY run_id DESC"""
        else:
            sql = f"SELECT * FROM {runs} ORDER BY run_id DESC"
    elif kind == "reports":
        sql = f"""SELECT report_id AS id,as_of,data_as_of,mode,payload->>'summary' summary
            FROM {db.qualified("agent_research")} ORDER BY as_of DESC,report_id DESC"""
    elif kind == "decisions":
        sql = f"""SELECT d.* FROM {db.qualified("pod_decision")} d
            JOIN {db.qualified("pod")} p USING(pod_id) WHERE p.strategy_key='agent_manager'
            ORDER BY d.trade_date DESC"""
    elif kind == "forecasts":
        conditions = ["TRUE"]
        if horizon:
            conditions.append("horizon_days = %s")
            params.append(horizon)
        if status:
            conditions.append(
                "scored_at IS NULL" if status == "pending" else "scored_at IS NOT NULL"
            )
        sql = f"""SELECT forecast_id AS id,board,proxy_code,issued_at,horizon_days,base_date,
            target_date,prob_up,expected_return,samples,confidence,realized_return,hit,
            CASE WHEN scored_at IS NULL THEN 'pending' ELSE 'scored' END status
            FROM {db.qualified("agent_board_forecast")} WHERE {" AND ".join(conditions)}
            ORDER BY issued_at DESC,board,horizon_days,forecast_id"""
    elif kind == "events":
        run_detail = "stats->>'summary',stats->>'stage',''" if public else "error,stats->>'summary',stats->>'stage',''"
        sql = f"""SELECT * FROM (
            SELECT started_at ts, 'run' AS kind, phase AS title,
                   coalesce({run_detail}) AS detail,
                   status, run_id, NULL::int pod_id, run_id AS id FROM {runs}
            UNION ALL
            SELECT e.created_at, 'lifecycle', p.name, e.reason, e.event, NULL, e.pod_id,e.event_id
            FROM {db.qualified("pod_event")} e JOIN {db.qualified("pod")} p USING(pod_id)
            UNION ALL
            SELECT d.created_at, 'decision', p.name || ' · ' || d.summary,
                   d.rationale, d.action, NULL, d.pod_id,d.decision_id
            FROM {db.qualified("pod_decision")} d JOIN {db.qualified("pod")} p USING(pod_id)
            UNION ALL
            SELECT o.created_at, 'order', p.name || ' · ' || i.name, o.reason, o.side,NULL,o.pod_id,o.order_id
            FROM {db.qualified("pod_order")} o JOIN {db.qualified("instrument")} i USING(instrument_id)
            JOIN {db.qualified("pod")} p USING(pod_id)) e ORDER BY ts DESC,kind,id DESC"""
    else:
        raise ValueError("未知历史类型")
    rows = db.query(sql + " LIMIT %s OFFSET %s", [*params, limit + 1, offset])
    return {"items": queries.clean_rows(rows[:limit]), "has_more": len(rows) > limit}


def dashboard(report_id=None, *, public=False):
    settings = load_settings()
    result = research.dashboard_payload(report_id)
    manager = db.query_one(f"""SELECT p.pod_id,p.pod_no,p.name,p.initial_capital,
        coalesce(n.cash,p.initial_capital) cash, coalesce(n.market_value,0) market_value,
        coalesce(n.total_value,p.initial_capital) total_value,n.cum_return,n.drawdown,n.trade_date last_date
        FROM {db.qualified("pod")} p LEFT JOIN LATERAL (SELECT * FROM {db.qualified("pod_nav")}
        WHERE pod_id=p.pod_id ORDER BY trade_date DESC LIMIT 1) n ON true
        WHERE p.strategy_key='agent_manager'""")
    if manager:
        manager = queries.clean_rows([manager])[0]
        manager["holdings"] = queries._holdings(manager["pod_id"])
        manager["history"] = queries._history(manager["pod_id"])
    news = db.query(f"""SELECT news_id,published_at,title,body,source,url FROM {db.qualified("news_item")}
        WHERE published_at <= now() ORDER BY published_at DESC,news_id DESC LIMIT 30""")
    model = os.getenv("LLM_MODEL_DEEP", "").strip() or llm.MODEL
    return {
        **result,
        "status": {
            "llm_configured": bool(settings.llm.enabled and llm.BASE_URL and llm.API_KEY and model),
            "watch_interval_minutes": 60,
            "model": model,
            "timezone": settings.timezone,
            "now": now(),
            "schedule": {
                k: settings.schedule[k]
                for k in (
                    "morning_nav_retry",
                    "premarket_news",
                    "decision_prepare",
                    "intraday_decide",
                    "decision_retry",
                    "decision_publish_by",
                    "ingest_market",
                    "settle",
                    "score",
                    "late_nav_retry",
                )
            },
            **scheduler_state(),
        },
        "manager": manager,
        "news": news,
        **{kind: history(kind, public=public)["items"] for kind in ("runs", "events", "orders", "decisions")},
    }
