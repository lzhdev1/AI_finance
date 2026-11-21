"""Application, registration and earliest redemption dates for the online paper book."""

from datetime import time

from . import db
from .config import load_settings

CMF_BANK_PROSPECTUS = "https://static.cmfchina.com/fundarticle/20260519/20023023257605.pdf#page=36"


def sessions(from_day, *, include_start=True, count=9, known_by=None):
    operator = ">=" if include_start else ">"
    return [
        r["trade_date"]
        for r in db.query(
            f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date {operator} %s "
            "AND ingested_at<=coalesce(%s::timestamptz,'infinity') ORDER BY trade_date LIMIT %s",
            [from_day, known_by, count],
        )
    ]


def clock(code, days):
    start = load_settings().schedule["intraday_decide"]
    verified = code == "014028"
    return {
        "decision_window": f"{start} ≤ 上海时间 < 15:00",
        "pricing": "开放日 T 截止前冻结指令，按 T 日收盘后的单位净值计价",
        "confirmation_lag": 1,
        "redeem_lag": 2,
        "rule_source": "招商中证银行指数基金 2026 年第二号招募说明书，第 36 页"
        if verified
        else "模拟账户默认：按境内交易日历推算，未逐产品核验",
        "source_url": CMF_BANK_PROSPECTUS if verified else None,
        "apply_date": str(days[0]) if days else None,
        "confirm_date": str(days[1]) if len(days) > 1 else None,
        "earliest_redeem_date": str(days[2]) if len(days) > 2 else None,
        "redemptions_available_date": (str(days[8]) if len(days) > 8 else None)
        if verified
        else (str(days[1]) if len(days) > 1 else None),
        "payout": (
            "模拟赎回款在 T+7 交易日结束后释放，下一交易日可再申购"
            if verified
            else "沿用模拟到账假设：当日赎回净值已入账后，下轮可申购"
        ),
    }


def next_application_sessions(as_of):
    return sessions(as_of.date(), include_start=as_of.time() < time(15), known_by=as_of)


def redeemable(account, trade_day):
    # Both the verified bank product and the paper default release at T+2.
    previous = db.query(
        f"SELECT trade_date FROM {db.qualified('trade_calendar')} WHERE trade_date < %s "
        "ORDER BY trade_date DESC LIMIT 2",
        [trade_day],
    )
    cutoff = previous[-1]["trade_date"] if len(previous) == 2 else None
    return {
        code: sum(lot.shares for lot in lots if cutoff is not None and lot.buy_date <= cutoff)
        for code, lots in account.lots.items()
    }


def cash_receivable(pod_id, trade_day):
    # The prospectus specifies a payment deadline, not an arrival timestamp.
    # Keep NAV accounting at trade date and freeze proceeds for new purchases.
    row = db.query_one(
        f"""SELECT coalesce(sum(o.amount-o.fee),0)::float waiting
            FROM {db.qualified("pod_order")} o JOIN {db.qualified("instrument")} i USING(instrument_id)
            JOIN {db.qualified("pod_decision")} d
              ON d.pod_id=o.pod_id AND d.trade_date=o.decided_on
            WHERE o.pod_id=%s AND i.code='014028' AND o.side='sell' AND o.execute_on<=%s
              AND d.payload->>'otc_clock_version'='1'
              AND NOT EXISTS (SELECT 1 FROM {db.qualified("trade_calendar")} c
                  WHERE c.trade_date>o.execute_on AND c.trade_date<%s OFFSET 6)""",
        [pod_id, trade_day, trade_day],
    )
    return row["waiting"]
