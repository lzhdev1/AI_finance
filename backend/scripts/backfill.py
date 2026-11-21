from __future__ import annotations

import argparse

from quantlab.calendar import setup_logging
from quantlab.config import load_universe
from quantlab.data import quality_report, update_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="全量抓取基金池净值与交易日历")
    parser.add_argument("--no-quality", action="store_true", help="不打印数据质量报告")
    args = parser.parse_args()

    setup_logging()
    universe = load_universe()
    written = update_universe(universe)
    print("写入行数：")
    for key, value in written.items():
        print(f"  {key}: {value}")

    if not args.no_quality:
        print("\n数据质量：")
        report = quality_report(universe)
        print(report.to_string(index=False) if not report.empty else "无数据")


if __name__ == "__main__":
    main()