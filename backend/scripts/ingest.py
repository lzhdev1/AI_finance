"""数据抓取入口。

    docker compose run --rm scheduler python scripts/ingest.py calendar
    docker compose run --rm scheduler python scripts/ingest.py funds
    docker compose run --rm scheduler python scripts/ingest.py news
    docker compose run --rm scheduler python scripts/ingest.py boards
    docker compose run --rm scheduler python scripts/ingest.py quotes --limit 20
    docker compose run --rm scheduler python scripts/ingest.py flows
    docker compose run --rm scheduler python scripts/ingest.py market
    docker compose run --rm scheduler python scripts/ingest.py index
    docker compose run --rm scheduler python scripts/ingest.py status

一次抓全部会用 all；遇到限流会自动停在该阶段（pipeline_run 记为 partial），
下次补抓即可，不会因为重试而把封禁时间拖长。
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from quantlab import ingest
from quantlab.calendar import setup_logging
from quantlab.config import load_settings

ACTIONS = ["calendar", "funds", "news", "boards", "quotes", "flows", "market", "index", "all", "status"]


def show_status() -> None:
    print("各表最新日期：")
    for table, latest in ingest.freshness().items():
        print(f"  {table:<18} {latest}")
    print("\n净值质量检查：")
    for item in ingest.check_nav_quality():
        print(f"  {item['target']:<24} {item['status']:<5} lag={item['metric']} {item['detail']}")
    print("\n行数：")
    for table, count in sorted(ingest.db.table_counts().items()):
        print(f"  {table:<20} {count}")


def run_all() -> None:
    ingest.update_calendar()
    ingest.update_fund_nav()
    ingest.update_news()
    ingest.update_boards()
    ingest.update_index()
    ingest.update_market_flow()
    ingest.update_board_quote(limit=20)
    ingest.update_board_flow(indicator="今日", sector_type="行业资金流", period="1d")
    ingest.update_board_flow(indicator="今日", sector_type="概念资金流", period="1d")
    ingest.record_quality(date.today(), ingest.check_nav_quality())


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantLab 数据抓取")
    parser.add_argument("what", choices=ACTIONS, help="要抓取的数据域")
    parser.add_argument("--limit", type=int, default=20, help="单次最多抓多少个板块（quotes）")
    parser.add_argument("--codes", nargs="*", default=None, help="限定基金代码（funds）")
    parser.add_argument("--sources", nargs="*", default=None, help="限定新闻源（news）")
    args = parser.parse_args()

    setup_logging(level=logging.INFO)
    ingest.db.wait_for_db()

    if args.what == "status":
        show_status()
        return
    if args.what == "all":
        run_all()
    elif args.what == "calendar":
        ingest.update_calendar()
    elif args.what == "funds":
        ingest.update_fund_nav(args.codes)
    elif args.what == "news":
        ingest.update_news(args.sources)
    elif args.what == "boards":
        ingest.update_boards()
    elif args.what == "quotes":
        ingest.update_board_quote(limit=args.limit)
    elif args.what == "flows":
        settings = load_settings()
        for kind, sector_type in (("industry", "行业资金流"), ("concept", "概念资金流")):
            try:
                ingest.update_board_flow(indicator="今日", sector_type=sector_type, period="1d")
            except Exception as exc:  # noqa: BLE001
                print(f"{kind} 资金流抓取失败：{exc}")
    elif args.what == "market":
        ingest.update_market_flow()
    elif args.what == "index":
        ingest.update_index()

    print("\n当前状态：")
    show_status()


if __name__ == "__main__":
    main()