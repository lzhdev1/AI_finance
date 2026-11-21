from __future__ import annotations

import argparse

from quantlab.calendar import setup_logging
from quantlab.config import load_portfolio, load_settings, load_universe
from quantlab.notify import send_dingtalk
from quantlab.signals import build_strategy, generate_signal, save_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="生成一次调仓信号")
    parser.add_argument("--lookback", type=int, default=60)
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--no-absolute", action="store_true", help="关闭绝对动量过滤")
    parser.add_argument("--notify", action="store_true", help="推送到钉钉")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    universe = load_universe()
    strategy = build_strategy(
        universe,
        lookback=args.lookback,
        top_n=args.top_n,
        use_absolute=not args.no_absolute,
    )
    report = generate_signal(universe, load_portfolio(), strategy, settings=settings)
    md_path, json_path = save_signal(report, settings=settings)
    print(report.to_markdown())
    print(f"已写入 {md_path}")
    print(f"已写入 {json_path}")
    if args.notify:
        ok = send_dingtalk(f"调仓信号 {report.as_of}", report.to_markdown())
        print("钉钉推送成功" if ok else "钉钉未配置或推送失败")


if __name__ == "__main__":
    main()