"""把板块链接到可投基金（instrument_board）。

做法不神秘：**基金名称里带板块名的场外 C 类指数基金**。
例如板块「半导体」→「华夏国证半导体芯片ETF联接C」。

这是启发式匹配，不是权威映射，所以：
* 只取每个板块 1 只（优先带"指数/联接/ETF"字样的 C 类）；
* 结果标记 source='name-match'，方便以后换成人工维护或相关性验证；
* 匹配不到就留空，宁可没映射，也不要塞一只不相关的基金进去。
"""

from __future__ import annotations

import argparse
from quantlab.fund_universe import link_funds

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="板块与场外 C 类指数基金映射")
    parser.add_argument("--kind", default="industry")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    link_funds(args.kind, args.limit, args.dry_run)
