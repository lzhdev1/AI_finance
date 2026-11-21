"""上海时间定时调研、模拟指令、净值确认与研究任务队列。"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, ingest, runner, station
from .calendar import setup_logging
from .config import load_settings

log = logging.getLogger(__name__)


def _request(trigger):
    try:
        return station.queue_research(trigger)
    except station.ResearchBusy:
        log.info("研究任务已在队列中")
        return None


def job_news():
    _request("morning")


def job_watch():
    """Watch for newly visible evidence at night and on holidays as well as during sessions."""
    with ingest.phase("market_watch") as stats:
        news = {}
        stats["inserted_news"] = ingest.update_news(audit=news)
        if news.get("warnings"):
            stats["warnings"] = news["warnings"]
        stamp = station.now()
        # Keep the decision worker and the fixed morning/closing research slot available.
        if stamp.hour in (8, 14, 18):
            stats["summary"] = "固定研究时段由当班任务接续"
            # The morning job runs seven days. Closing and decision jobs run on trading days.
            if stamp.hour == 8 or runner.is_trade_day(stamp.date()):
                return
        reason = station.watch_evidence(stamp)
        if reason:
            request = _request("watch")
            stats["summary"] = reason if request else "已有研究任务处理本轮信息"
            if request:
                stats["research_run_id"] = request["run_id"]
        else:
            stats["summary"] = "已核对新闻、行业行情和资金流，暂无新资料触发研究"


def job_ingest():
    from .fund_universe import link_funds

    def discover():
        if not db.query_one(f"SELECT 1 FROM {db.qualified('instrument_board')} LIMIT 1"):
            with ingest.phase("fund_mapping") as stats:
                stats["matched"] = link_funds()

    today = station.now().date()
    steps = [
        ingest.update_calendar,
        ingest.update_boards,
        discover,
        ingest.update_fund_nav,
        ingest.update_news,
        ingest.update_index,
        lambda: ingest.update_board_quote(limit=None, kinds=["industry"]),
        lambda: ingest.update_board_quote(limit=None, kinds=["concept"]),
    ]
    if runner.is_trade_day(today) and station.now().hour >= 15:
        steps.extend(
            [
                ingest.update_market_flow,
                ingest.update_board_flow,
                lambda: ingest.update_board_flow(sector_type="概念资金流"),
                lambda: ingest.update_board_flow(indicator="5日", period="5d"),
                lambda: ingest.update_board_flow(indicator="5日", period="5d", sector_type="概念资金流"),
            ]
        )
    for step in steps:
        try:
            step()
        except Exception:
            log.exception("采集步骤失败，继续处理其他数据集")
    ingest.record_quality(today, ingest.check_nav_quality())
    _request("market_close")


def job_intraday():
    with ingest.phase("manager_decision") as stats:
        result = runner.run_intraday()
        stats.update(result)
        if result.get("status") in {"cutoff", "blocked"}:
            stats["warnings"] = [result["summary"]]


def job_snapshot():
    from . import realtime

    if runner.is_trade_day(station.now().date()):
        with ingest.phase("market_snapshot") as stats:
            result = realtime.snapshot()
            stats.update(boards=result["boards"], indexes=result["indexes"])
            if not result["boards"]:
                stats["warnings"] = ["本次未取得板块行情"]


def job_settle():
    """净值可能于晚间或次日才发布，优先补经理已持有或已下指令的基金。"""
    with ingest.phase("manager_settle") as stats:
        pod = station.ensure_manager()
        from . import fund_forecasts, pod_store

        codes = set(pod_store.load_account(pod).codes())
        pending = db.query(
            f"""SELECT payload FROM {db.qualified("pod_decision")}
            WHERE pod_id=%s AND executed_at IS NULL""",
            [pod["pod_id"]],
        )
        for row in pending:
            codes.update(o["code"] for o in (row["payload"] or {}).get("pending", []))
        missing = fund_forecasts.missing_nav_dates(station.now())
        codes.update(missing)
        if codes:
            try:
                ingest.update_fund_nav(sorted(codes), required_dates=missing)
            except Exception as exc:
                stats["warnings"] = [f"基金净值补采失败 · {type(exc).__name__}"]
        result = runner.run_settle()
        stats.update(result)
        unresolved = [r for r in result["settled"] if r["status"] in ("waiting_nav", "blocked")]
        if unresolved or result.get("valuation_gap"):
            stats.setdefault("warnings", []).append("指令等待净值或账本核查")


def job_score():
    from . import research

    with ingest.phase("forecast_review") as stats:
        counts = research.settle_forecast_counts(station.now())
        stats.update(scored=counts["boards"], funds_scored=counts["funds"])


def bootstrap():
    """新库启动或停机后恢复信息采集，不补发错过的申赎指令。"""
    today = station.now().date()
    latest = runner.latest_trade_date(
        today - timedelta(days=1) if station.now().hour < 15 else today
    )
    fresh = ingest.freshness()
    if (
        not latest
        or latest < today - timedelta(days=14)
        or any(not fresh[k] or fresh[k] < latest for k in ("instrument_nav", "board_quote"))
    ):
        job_ingest()
    elif not db.query_one(
        f"SELECT 1 FROM {db.qualified('agent_research')} WHERE as_of::date=%s", [today]
    ):
        _request("startup")
    job_settle()


JOBS = {
    "news": job_news,
    "watch": job_watch,
    "ingest": job_ingest,
    "intraday": job_intraday,
    "snapshot": job_snapshot,
    "settle": job_settle,
    "score": job_score,
    "research": station.process_queue,
}


def build_scheduler():
    settings = load_settings()
    scheduler = BlockingScheduler(
        timezone=settings.timezone,
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 120},
    )

    def at(name, func, value, days="mon-fri"):
        hour, minute = map(int, value.split(":"))
        scheduler.add_job(
            func,
            CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=settings.timezone),
            id=name,
        )

    at("premarket_news", job_news, settings.schedule["premarket_news"], "mon-sun")
    at("ingest_market", job_ingest, settings.schedule["ingest_market"])
    at("intraday_decide", job_intraday, settings.schedule["intraday_decide"])
    at("decision_retry", job_intraday, settings.schedule["decision_retry"])
    at("settle", job_settle, settings.schedule["settle"])
    at("late_nav_retry", job_settle, settings.schedule["late_nav_retry"])
    at("morning_nav_retry", job_settle, settings.schedule["morning_nav_retry"])
    at("forecast_review", job_score, settings.schedule["score"])
    scheduler.add_job(job_watch, "cron", minute=5, id="market_watch")
    scheduler.add_job(
        job_snapshot,
        "cron",
        day_of_week="mon-fri",
        hour="9-11,13",
        minute=30,
        id="market_snapshots",
    )
    scheduler.add_job(
        job_snapshot, "cron", day_of_week="mon-fri", hour=14, minute="0,20", id="preclose_snapshot"
    )
    scheduler.add_job(station.heartbeat, "interval", seconds=20, id="heartbeat")
    scheduler.add_job(station.process_queue, "interval", seconds=6, id="research_queue")
    scheduler.add_job(bootstrap, "date", run_date=station.now(), id="recover")
    return scheduler


def main():
    parser = argparse.ArgumentParser(description="QuantLab 调度器")
    parser.add_argument("--once", choices=sorted(JOBS), default=None)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    setup_logging(level=logging.INFO)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.ERROR)
    if args.list:
        for job in build_scheduler().get_jobs():
            print(job.id, job.trigger)
        return
    db.wait_for_db()
    station.ensure_manager()
    if args.once:
        JOBS[args.once]()
        return
    station.heartbeat()
    build_scheduler().start()


if __name__ == "__main__":
    main()
