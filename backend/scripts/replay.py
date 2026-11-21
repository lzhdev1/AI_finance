"""历史回放：按交易日逐日跑一遍决策流程。

用途不是"回测收益率"，而是**演练规则本身**：
止损会不会误伤、冷却期够不够长、预测评分是否在按天累积——
这些都是"上线后才发现"的问题，回放能提前暴露。

    docker compose exec -e PGDATABASE=quantlab_replay_legacy -T api python scripts/replay.py --start 2026-09-01 --end 2026-10-29
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from quantlab import db, runner
from quantlab.calendar import setup_logging


def trading_days(start: date, end: date) -> list[date]:
    rows = db.query(
        f"SELECT trade_date FROM {db.qualified('trade_calendar')} "
        "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
        [start, end],
    )
    return [row["trade_date"] for row in rows]


def reset_pods() -> None:
    with db.transaction():
        runner.assert_replay_database()
        db.execute(f"DELETE FROM {db.qualified('pod')} WHERE strategy_key<>'agent_manager'")
    logging.getLogger(__name__).warning("已清空此独立实验库中的旧策略仓")


def main() -> None:
    parser = argparse.ArgumentParser(description="历史回放演练")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--reset", action="store_true", help="回放前清空所有仓")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    setup_logging(level=logging.WARNING if args.quiet else logging.INFO)
    db.wait_for_db()
    runner.assert_replay_database()

    if args.reset:
        reset_pods()

    days = trading_days(date.fromisoformat(args.start), date.fromisoformat(args.end))
    print(f"回放 {len(days)} 个交易日：{days[0]} ~ {days[-1]}")

    for day in days:
        # as_of=day：新闻窗口也必须停在回放日，否则会读到"明天"的消息
        result = runner.run_daily(trade_date=day, as_of=day)
        picks = result["picks"]
        best = picks[0] if picks else None
        line = f"{day} 卡={len(result['cards'])} 事件={[e['action'] for e in result['lifecycle']]}"
        if best:
            line += f" 最看好=#{best['pod_no']}"
        print(line)

    print("\n最终状态：")
    for pod in db.query(
        f"""
        SELECT p.pod_no, p.name, p.status, p.opened_on, p.closed_on,
               n.unit_nav, n.cum_return, n.drawdown, n.trade_date
        FROM {db.qualified('pod')} p
        LEFT JOIN LATERAL (
            SELECT * FROM {db.qualified('pod_nav')} WHERE pod_id = p.pod_id
            ORDER BY trade_date DESC LIMIT 1
        ) n ON TRUE
        ORDER BY p.pod_no
        """
    ):
        nav = f"{float(pod['unit_nav']):.4f}" if pod["unit_nav"] else "—"
        cum = f"{float(pod['cum_return']):+.2%}" if pod["cum_return"] is not None else "—"
        print(
            f"  仓 #{pod['pod_no']:<2} {pod['name']:<10} {pod['status']:<8} "
            f"净值 {nav} 累计 {cum} 开 {pod['opened_on']} 关 {pod['closed_on']}"
        )

    row = db.query_one(
        f"SELECT count(*) AS n, avg(CASE WHEN hit THEN 1.0 ELSE 0.0 END) AS rate, "
        f"avg(brier) AS brier FROM {db.qualified('pod_prediction')} WHERE hit IS NOT NULL"
    )
    if row and row["n"]:
        print(f"\n预测结算 {row['n']} 条，命中率 {float(row['rate']):.1%}，Brier {float(row['brier']):.3f}")


if __name__ == "__main__":
    main()
