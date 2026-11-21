"""探查 akshare 接口是否可用、实际列名是什么。

akshare 的接口名和返回列会随版本变化，抓取层不能靠猜。
这个脚本把「我们要用的接口」逐个打一遍，把真实列名和样例行打出来，
再据此写 ingest 代码。

    docker compose run --rm scheduler python scripts/probe_sources.py
"""

from __future__ import annotations

import traceback

import akshare as ak

CANDIDATES = [
    # 交易日历
    "tool_trade_date_hist_sina",
    # 板块清单
    "stock_board_industry_name_em",
    "stock_board_concept_name_em",
    # 板块历史行情
    "stock_board_industry_hist_em",
    "stock_board_concept_hist_em",
    # 板块资金流（当日排名）
    "stock_sector_fund_flow_rank",
    # 板块资金流（单板块历史）
    "stock_sector_fund_flow_hist",
    # 大盘资金流
    "stock_market_fund_flow",
    # 个股资金流排名（用于聚合到板块）
    "stock_individual_fund_flow_rank",
    # 新闻
    "stock_info_global_cls",
    "stock_info_global_em",
    # 基金
    "fund_open_fund_info_em",
    "fund_name_em",
    "fund_etf_spot_em",
    "fund_etf_hist_em",
    # 指数
    "stock_zh_index_daily_em",
    "index_zh_a_hist",
    # 两融
    "stock_margin_sse",
]


def report(name: str) -> None:
    func = getattr(ak, name, None)
    if func is None:
        print(f"[缺失] {name}")
        return
    print(f"[存在] {name}")
    probes: list[dict] = []
    if name == "stock_sector_fund_flow_rank":
        probes = [
            {"indicator": "今日", "sector_type": "行业资金流"},
            {"indicator": "今日", "sector_type": "概念资金流"},
            {"indicator": "5日", "sector_type": "行业资金流"},
        ]
    elif name in ("stock_board_industry_hist_em", "stock_board_concept_hist_em"):
        probes = [
            {
                "symbol": "小金属" if "industry" in name else "锂电池",
                "period": "日k",
                "start_date": "20260101",
                "end_date": "20261231",
                "adjust": "",
            }
        ]
    elif name == "stock_sector_fund_flow_hist":
        probes = [{"symbol": "小金属"}]
    elif name == "fund_open_fund_info_em":
        probes = [{"symbol": "007339", "indicator": "单位净值走势"}]
    elif name == "fund_etf_hist_em":
        probes = [
            {"symbol": "510300", "period": "daily", "start_date": "20260101", "end_date": "20261231", "adjust": ""}
        ]
    elif name == "stock_zh_index_daily_em":
        probes = [{"symbol": "sh000300"}]
    elif name == "index_zh_a_hist":
        probes = [{"symbol": "000300", "period": "daily", "start_date": "20260101", "end_date": "20261231"}]
    elif name == "stock_info_global_cls":
        probes = [{"symbol": "全部"}]
    else:
        probes = [{}]

    for kwargs in probes:
        label = f"{name}({kwargs})" if kwargs else name
        try:
            df = func(**kwargs)
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {label}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=1)
            continue
        if df is None or len(df) == 0:
            print(f"  [空] {label}")
            continue
        print(f"  [成功] {label} rows={len(df)}")
        print(f"    columns={list(df.columns)}")
        print(f"    head={df.head(2).to_dict(orient='records')}")


def main() -> None:
    print("akshare", ak.__version__)
    for name in CANDIDATES:
        report(name)
        print()


if __name__ == "__main__":
    main()