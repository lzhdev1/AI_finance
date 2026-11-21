"""Check the upstream functions used by container jobs without network requests."""

import akshare as ak


def test_market_and_news_connectors_match_installed_akshare():
    # The 1.20.41 rename removed several of these exported names.
    names = (
        "stock_info_global_cls", "stock_info_global_em", "fund_name_em",
        "stock_board_industry_name_ths", "stock_board_industry_index_ths",
        "stock_board_concept_name_ths", "stock_board_concept_index_ths",
        "stock_fund_flow_industry", "stock_fund_flow_concept", "stock_zh_index_daily",
        "tool_trade_date_hist_sina", "fund_open_fund_info_em",
    )
    assert all(callable(getattr(ak, name, None)) for name in names)
