from copy import deepcopy

import pytest

from quantlab import flow_claims, research


def _sector(bid, name, net):
    return {
        "board_id": bid,
        "board": name,
        "as_of": "2026-11-20",
        "evidence_ids": [f"B{bid}", f"G{bid}", f"I{bid}"],
        "capital_flow": {
            "flow_status": "available",
            "flow_date": "2026-11-20",
            "flow_source": "同花顺",
            "net_yuan": net,
            "net_pct": 2.35 if net > 0 else -2.35,
            "net_5d_yuan": -200_000_000,
            "net_5d_pct": -1.38,
        },
        "intraday": {
            "at": "2026-11-23T14:20:00+08:00",
            "capital_flow": {
                "source": "东方财富板块行情",
                "net_yuan": -600_000,
                "net_pct_points": -0.35,
            },
        },
    }


BANK = _sector(81, "银行", 125_000_000)
BROKER = _sector(86, "证券", -125_000_000)


@pytest.mark.parametrize(
    ("text", "refs", "allowed"),
    [
        ("银行净流入1.25亿元", ["G81"], True),
        ("银行资金流入12500万元", ["G81"], True),
        ("银行净流入超过1亿元", ["G81"], True),
        ("银行净流入不高于1.25亿元", ["G81"], True),
        ("银行净流入1至2亿元", ["G81"], True),
        ("银行净流入1亿至2亿元", ["G81"], True),
        ("银行净流入125,000,000元", ["G81"], True),
        ("银行净流入超过1.25亿元", ["G81"], False),
        ("银行净流入0.1亿至1亿元", ["G81"], False),
        ("银行净流入999999亿元", ["G81"], False),
        ("银行流入999999亿元", ["G81"], False),
        ("银行净流入，其金额达999999亿元", ["G81"], False),
        ("银行净流入50万元", ["G81"], False),
        ("银行净流出1.25亿元", ["G81"], False),
        ("银行净流入1.25亿元", ["B81"], False),
        ("银行净流入1.25亿元", ["G86"], False),
        ("证券净流出1.25亿元", ["G86"], True),
        ("银行主力净流入1.25亿元", ["G81"], False),
        ("同花顺银行净流入1.25亿元", ["G81"], True),
        ("东方财富银行净流入1.25亿元", ["G81"], False),
        ("银行近五日累计净流出2亿元", ["G81"], True),
        ("银行近5个交易日净占比-1.38%", ["G81"], True),
        ("银行近五日累计净流入1.25亿元", ["G81"], False),
        ("银行近五日净流出60万元", ["I81"], False),
        ("银行盘中主力净流出60万元", ["I81"], True),
        ("银行盘中资金净流出60万元", ["G81"], False),
        ("银行收盘净流出60万元", ["I81"], False),
        ("银行今日净流入1.25亿元", ["G81"], False),
        ("银行今日盘中资金净流出占比0.35%", ["I81"], True),
        ("银行净占比2.35%", ["G81"], True),
        ("银行净占比99%", ["G81"], False),
        ("银行和证券均净流入", ["G81", "G86"], False),
        ("另一行业净流入1.25亿元", ["G81"], False),
        ("银行连续三天净流入", ["G81"], False),
    ],
)
def test_each_flow_fact_matches_its_cited_sector_period_direction_and_units(text, refs, allowed):
    assert flow_claims.supported(text, [BANK, BROKER], refs, decision_day="2026-11-23") is allowed


def test_unobserved_bank_inflow_cannot_be_published_using_only_an_actual_quote_ref():
    bank = deepcopy(BANK)
    bank.update(evidence_ids=["B81"], capital_flow=None, intraday=None)
    p = {
        "stance": "reduced",
        "watch": [{"board_id": 81, "intent": "observe", "reason": "动量回升", "refs": ["B81"]}],
        "thesis": "银行量价回升",
        "risk": "若银行资金净流出超过1亿元则重新评估",
        "thesis_refs": ["B81"],
        "_presented": {"boards": {81: ["B81"]}, "macro_refs": []},
    }
    assert research._selection([bank], p)
    for field in ("reason", "thesis", "risk"):
        bad = deepcopy(p)
        (bad["watch"][0] if field == "reason" else bad)[field] = "银行获资金净流入999999亿元"
        assert research._selection([bank], bad) is None
        # Frozen replay versions used the original evidence/percentage validation.
        assert research._selection([bank], bad, replay=True)


def test_stale_daily_and_other_sector_evidence_cannot_support_a_watch_item():
    bank = deepcopy(BANK)
    bank["capital_flow"]["flow_date"] = "2026-11-19"
    assert not flow_claims.supported("银行净流入", [bank], ["G81"])
    assert not flow_claims.supported("证券净流出", [BANK, BROKER], ["G86"], subject=BANK)
    assert not flow_claims.supported("净流入", [BANK, BROKER], ["G81", "G86"])
    assert not flow_claims.supported("若调整发生但银行已净流入999亿元", [BANK], ["B81"], risk=True)
