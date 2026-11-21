"""Run or resume the production paper manager in a dedicated historical database."""

import argparse
import json
import re
from datetime import date, datetime, time, timedelta

from quantlab import db, pod_store, replay_archive, replay_comparison, research, runner
from quantlab.replay_universe import apply_as_of
from quantlab.replay_verification import verify_missing


def account_for(start, end, database, version):
    evidence = {
        "mode": "historical_replay",
        "run_id": database,
        "start": str(start),
        "end": str(end),
    }
    if version == "v2":
        evidence["strategy_version"] = version
    accounts = pod_store.list_pods()
    if accounts:
        if (
            len(accounts) != 1
            or accounts[0]["strategy_key"] != "agent_manager"
            or accounts[0]["entry_evidence"] != evidence
        ):
            raise ValueError("回放库已有其他账户或区间，请选用新的独立数据库")
        return accounts[0]
    pod_id = pod_store.create_pod(
        name="基金经理 · 半年独立回放" if version == "v1" else "基金经理 V2 · 半年独立回放",
        strategy_key="agent_manager",
        thesis="按板块研究、基金映射和已到期预测配置模拟资金。",
        invalidation_rules=["数据不足时保持原仓", "风险约束优先于研究目标"],
        entry_evidence=evidence,
        trade_date=start,
        benchmark_code="000300",
    )
    return pod_store.get_pod(pod_id)


def run(start, end, version="v1"):
    database = db.query_one("SELECT current_database() AS name")["name"]
    if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", database):
        raise ValueError("回放必须连接专用的 quantlab_replay_* 数据库")
    if end < start or end >= datetime.now(research.SH).date():
        raise ValueError("请使用已经结束的历史区间")
    try:
        published = replay_archive.current(replay_archive.version_path(version))["summary"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Only the derived file can be rebuilt. Require the existing account's identity
        # before opening or publishing anything into this version slot.
        accounts = pod_store.list_pods()
        if len(accounts) != 1:
            raise
        manifest = accounts[0]["entry_evidence"]
        if (manifest.get("mode") != "historical_replay" or manifest.get("run_id") != database
                or manifest.get("strategy_version", "v1") != version
                or (manifest.get("start"), manifest.get("end")) != (str(start), str(end))):
            raise
        published = {"id": database}
    if published.get("id") and published["id"] != database:
        raise ValueError("该版本已有其他账户的回放归档")
    destination = (
        replay_archive.ARCHIVE if version == "v1" else replay_archive.version_path(version)
    )
    lock = db.query_one("SELECT pg_try_advisory_lock(794022005) AS acquired")
    if not lock["acquired"]:
        raise RuntimeError("该库已有回放进程运行")
    try:
        sessions = [
            r["trade_date"]
            for r in db.query(
                f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date BETWEEN %s AND %s ORDER BY 1",
                [start, end],
            )
        ]
        if not sessions:
            raise RuntimeError("回放库缺少交易日历")
        with db.transaction():
            pod = account_for(start, end, database, version)
        completed = [
            r["trade_date"]
            for r in db.query(
                f"SELECT trade_date FROM {db.qualified('pod_nav')} WHERE pod_id=%s ORDER BY 1",
                [pod["pod_id"]],
            )
        ]
        if completed != sessions[: len(completed)]:
            raise ValueError("已有估值不是回放区间的连续前缀")
        if len(completed) < len(sessions):
            replay_archive.publish(pod, sessions, path=destination)
        for i, day in enumerate(sessions[len(completed) :], len(completed) + 1):
            cutoff = datetime.combine(day, time(14, 30), research.SH)
            confirmation = datetime.combine(day + timedelta(days=1), time(8, 30), research.SH)
            # This complete session is also the restart checkpoint. No forward outcomes are
            # inserted into the ledger until the research and exact instructions are frozen.
            with db.transaction():
                apply_as_of(day)
                research.settle_replay_forecasts(cutoff)
                report = research.generate_replay_research(cutoff, strategy_version=version)
                card = runner.plan_pod(pod, day, {}, research_report=report, replay=True)
                if card["status"] != "planned":
                    raise RuntimeError(f"{day} 未成功冻结策略 {card}")
                result = runner.execute_pod(pod, day)
                if result.get("status") != "settled":
                    raise RuntimeError(f"{day} 无法按实际净值确认 {result}")
                db.execute(
                    f"UPDATE {db.qualified('pod_decision')} SET created_at=%s, executed_at=%s "
                    "WHERE pod_id=%s AND trade_date=%s",
                    [cutoff, confirmation, pod["pod_id"], day],
                )
                db.execute(
                    f"UPDATE {db.qualified('pod_order')} SET created_at=%s WHERE pod_id=%s AND execute_on=%s",
                    [confirmation, pod["pod_id"], day],
                )
                research.settle_replay_forecasts(datetime.combine(day, time(20, 30), research.SH))
                # No later session has closed at 08:30, including the day after the final session.
                research.settle_replay_forecasts(confirmation)
            print(f"{i}/{len(sessions)} {day} {report['mode']} fills={result['fills']}", flush=True)
            if i < len(sessions) and (i == 1 or i % 5 == 0):
                replay_archive.publish(pod, sessions, path=destination)
    finally:
        db.query_one("SELECT pg_advisory_unlock(794022005)")
    # Publication checks the sealed account under its own transaction and writer lock.
    try:
        print("期后行情复核", verify_missing(), flush=True)
    finally:
        replay_archive.publish(pod, sessions, path=destination)
    replay_comparison.publish_attribution(version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--version", choices=("v1", "v2"), default="v1")
    args = parser.parse_args()
    run(args.start, args.end, args.version)
