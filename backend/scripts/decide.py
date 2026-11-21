"""跑决策，三种模式：

    # 盘中决策：抓行情、生成经理研究报告并冻结当日模拟指令
    docker compose run --rm scheduler python scripts/decide.py --mode intraday

    # 20:30 晚间结算：净值出来后才真正成交，并给预测打分
    docker compose run --rm scheduler python scripts/decide.py --mode settle

    # 旧多仓演练只可在专用实验库运行
    docker compose exec -e PGDATABASE=quantlab_replay_legacy -T api python scripts/decide.py --mode full --date 2026-10-29
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date

from quantlab import runner
from quantlab.calendar import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantLab 每日决策")
    parser.add_argument("--mode", choices=["intraday", "settle", "full"], default="intraday")
    parser.add_argument("--date", default=None, help="指定交易日（默认取最新交易日）")
    parser.add_argument("--no-open", action="store_true", help="不允许开新仓")
    args = parser.parse_args()

    setup_logging(level=logging.INFO)
    runner.db.wait_for_db()

    trade_date = date.fromisoformat(args.date) if args.date else None
    if args.mode == "intraday":
        result = runner.run_intraday(trade_date=trade_date)
    elif args.mode == "settle":
        result = runner.run_settle(trade_date=trade_date)
    else:
        result = runner.run_daily(trade_date=trade_date, open_new=not args.no_open)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
