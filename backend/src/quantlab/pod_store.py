"""仓的持久化层：把内存账本（pods.PodAccount）与数据库对齐。

关键取舍：**订单是唯一账本**。
持仓、现金都不单独存"当前值"，而是每次从 ``pod_order`` 回放出来——
这样永远不会出现"持仓表和订单对不上"的经典问题，代价只是几十笔订单的重放。
``pod_position`` / ``pod_nav`` 是每日快照，只用于展示与统计，不作为事实来源。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd
from psycopg.types.json import Jsonb

from . import db
from .config import load_settings
from .pods import Fill, PodAccount, PodRules

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 配置与规则

def load_rules() -> PodRules:
    agent = load_settings().agent
    return PodRules(
        min_eval_days=agent.pod_min_eval_days,
        hard_stop_drawdown=agent.pod_hard_stop_drawdown,
        soft_retire_hit_rate=agent.pod_soft_retire_hit_rate,
        cooldown_days=agent.pod_cooldown_days,
        max_pods=agent.max_pods,
    )


def fee_book(codes: Sequence[str]) -> dict[str, dict]:
    """从 instrument_fee 读出费率表，结构 {code: {"buy": rate, "sell": [[days, rate], ...]}}。"""
    rows = db.query(
        f"""
        SELECT i.code, f.side, f.holding_days_max, f.rate
        FROM {db.qualified('instrument')} i
        JOIN {db.qualified('instrument_fee')} f USING (instrument_id)
        WHERE i.code = ANY(%s)
        ORDER BY i.code, f.side, f.holding_days_max
        """,
        [list(codes)],
    )
    book: dict[str, dict] = {}
    for row in rows:
        entry = book.setdefault(row["code"], {"buy": 0.0, "sell": []})
        if row["side"] == "buy":
            entry["buy"] = float(row["rate"])
        else:
            entry["sell"].append([int(row["holding_days_max"]), float(row["rate"])])

    # 数据缺口兜底：新接入的基金常常还没有费率记录。
    # C 类典型费率：申购 0，持有 < 7 天赎回收 1.5%，7 天以上 0。
    for code in codes:
        entry = book.setdefault(code, {"buy": 0.0, "sell": []})
        if not entry["sell"]:
            entry["sell"] = [[7, 0.015], [999_999, 0.0]]
    return book


def instrument_map(*, include_inactive: bool = False) -> dict[str, dict]:
    predicate = "" if include_inactive else "WHERE is_active"
    return {
        row["code"]: row
        for row in db.query(
            f"SELECT instrument_id, code, name, kind, share_class, role, is_active FROM {db.qualified('instrument')} "
            f"{predicate}"
        )
    }


# ---------------------------------------------------------------- 仓的读写

def list_pods(status: Sequence[str] | None = None) -> list[dict]:
    sql = [f"SELECT * FROM {db.qualified('pod')}"]
    params: list[object] = []
    if status:
        sql.append("WHERE status = ANY(%s)")
        params.append(list(status))
    sql.append("ORDER BY pod_no")
    return db.query(" ".join(sql), params)


def get_pod(pod_id: int) -> dict | None:
    return db.query_one(f"SELECT * FROM {db.qualified('pod')} WHERE pod_id = %s", [pod_id])


def next_pod_no() -> int:
    row = db.query_one(f"SELECT COALESCE(max(pod_no), 0) + 1 AS n FROM {db.qualified('pod')}")
    return int(row["n"])


def assert_legacy_replay_database() -> None:
    name = db.query_one("SELECT current_database() AS name")["name"]
    if not re.fullmatch(r"quantlab_replay_[a-z0-9_]+", name):
        raise RuntimeError("旧多仓实验只能使用独立的 quantlab_replay_* 数据库")
    if db.query_one(f"SELECT 1 FROM {db.qualified('pod')} WHERE strategy_key='agent_manager'"):
        raise RuntimeError("该库已归属基金经理回放，请为旧多仓实验使用其他独立库")


def create_pod(
    *,
    name: str,
    strategy_key: str,
    thesis: str,
    invalidation_rules: Sequence[str],
    entry_evidence: Mapping,
    trade_date: date,
    benchmark_code: str | None = None,
    capital: float | None = None,
) -> int:
    if strategy_key != "agent_manager":
        assert_legacy_replay_database()
    agent = load_settings().agent
    capital = float(capital or agent.pod_capital)
    benchmark_code = benchmark_code or load_settings().benchmark
    pod_no = next_pod_no()
    row = db.query_one(
        f"""
        INSERT INTO {db.qualified('pod')}
            (pod_no, name, strategy_key, thesis, entry_evidence, invalidation_rules,
             benchmark_code, initial_capital, cap_amount, status, opened_on, min_eval_days)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
        RETURNING pod_id
        """,
        [
            pod_no,
            name,
            strategy_key,
            thesis,
            Jsonb(dict(entry_evidence)),
            Jsonb(list(invalidation_rules)),
            benchmark_code,
            capital,
            capital,
            trade_date,
            agent.pod_min_eval_days,
        ],
    )
    pod_id = int(row["pod_id"])
    log_event(pod_id, "create", trade_date, reason=thesis, rule_triggered="", snapshot=dict(entry_evidence))
    return pod_id


def log_event(
    pod_id: int,
    event: str,
    trade_date: date,
    *,
    reason: str = "",
    rule_triggered: str = "",
    snapshot: Mapping | None = None,
) -> None:
    db.execute(
        f"INSERT INTO {db.qualified('pod_event')} (pod_id, event, trade_date, rule_triggered, reason, snapshot) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [pod_id, event, trade_date, rule_triggered, reason, Jsonb(dict(snapshot or {}))],
    )


def set_status(pod_id: int, status: str, trade_date: date, reason: str, rule: str = "") -> None:
    db.execute(
        f"UPDATE {db.qualified('pod')} SET status = %s, closed_on = CASE WHEN %s = 'retired' THEN %s ELSE closed_on END, "
        "close_reason = CASE WHEN %s = 'retired' THEN %s ELSE close_reason END WHERE pod_id = %s",
        [status, status, trade_date, status, reason, pod_id],
    )
    event = "retire" if status == "retired" else ("suspend" if status == "suspended" else "resume")
    log_event(pod_id, event, trade_date, reason=reason, rule_triggered=rule)


# ---------------------------------------------------------------- 账本回放

def load_account(pod: Mapping, as_of: date | None = None) -> PodAccount:
    """从订单历史回放出现金与持仓批次（FIFO 需要买入日期，所以必须回放）。"""
    rows = db.query(
        f"""
        SELECT o.order_id, o.side, o.execute_on, o.amount, o.shares, o.nav, o.fee, i.code
        FROM {db.qualified('pod_order')} o
        JOIN {db.qualified('instrument')} i USING (instrument_id)
        WHERE o.pod_id = %s AND o.execute_on IS NOT NULL
          AND (%s::date IS NULL OR o.execute_on <= %s)
        ORDER BY o.execute_on, o.order_id
        """,
        [pod["pod_id"], as_of, as_of],
    )
    codes = sorted(set(instrument_map()) | {row["code"] for row in rows})
    account = PodAccount(
        initial_capital=float(pod["initial_capital"]),
        cash=float(pod["initial_capital"]),
        fees=fee_book(codes),
        min_trade_amount=load_settings().min_trade_amount,
    )
    for row in rows:
        account.apply_fill(Fill(
            row["execute_on"], row["code"], row["side"], float(row["amount"]),
            float(row["shares"]), float(row["nav"]), float(row["fee"]),
        ))
    return account


def save_fills(pod_id: int, decided_on: date, execute_on: date, fills: Sequence[Fill], reason: str = "") -> None:
    if not fills:
        return
    mapping = instrument_map(include_inactive=True)
    db.execute_many(
        f"""
        INSERT INTO {db.qualified('pod_order')}
            (pod_id, decided_on, execute_on, instrument_id, side, amount, shares, nav, fee, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                pod_id,
                decided_on,
                execute_on,
                mapping[fill.code]["instrument_id"],
                fill.side,
                round(fill.amount, 2),
                round(fill.shares, 6),
                round(fill.nav, 6),
                round(fill.fee, 4),
                fill.reason or reason,
            )
            for fill in fills
        ],
    )


# ---------------------------------------------------------------- 每日快照

def benchmark_cum_return(benchmark_code: str, since: date, trade_date: date) -> float | None:
    quotes = db.qualified("index_quote")
    row = db.query_one(
        f"""SELECT closing.close / NULLIF(opening.close, 0) - 1 AS gain
            FROM {quotes} opening JOIN {quotes} closing USING (index_code)
            WHERE opening.index_code = %s AND closing.trade_date = %s
              AND opening.trade_date = (SELECT min(trade_date) FROM {db.qualified('trade_calendar')}
                                        WHERE trade_date BETWEEN %s AND %s)""",
        [benchmark_code, trade_date, since, trade_date],
    )
    return float(row["gain"]) if row and row["gain"] is not None else None


def save_snapshot(
    pod: Mapping,
    trade_date: date,
    account: PodAccount,
    prices: Mapping[str, float],
    codes_to_names: Mapping[str, str] | None = None,
) -> dict:
    total = account.total_value(prices)
    unit_nav = total / float(pod["initial_capital"]) if pod["initial_capital"] else 1.0

    previous = db.query_one(
        f"SELECT unit_nav, trade_date FROM {db.qualified('pod_nav')} WHERE pod_id = %s AND trade_date < %s "
        "ORDER BY trade_date DESC LIMIT 1",
        [pod["pod_id"], trade_date],
    )
    prev_nav = float(previous["unit_nav"]) if previous else 1.0
    previous_session = db.query_one(
        f"SELECT max(trade_date) AS day FROM {db.qualified('trade_calendar')} "
        "WHERE trade_date >= %s AND trade_date < %s", [pod["opened_on"], trade_date],
    )["day"]
    consecutive = previous_session == (previous["trade_date"] if previous else None)
    daily_return = unit_nav / prev_nav - 1.0 if consecutive and prev_nav else None

    history = db.query(
        f"SELECT max(unit_nav) AS peak FROM {db.qualified('pod_nav')} WHERE pod_id = %s AND trade_date <= %s",
        [pod["pod_id"], trade_date],
    )
    peak = max(float(history[0]["peak"] or 1.0), unit_nav, 1.0) if history else max(1.0, unit_nav)
    drawdown = unit_nav / peak - 1.0

    benchmark_start = pod["opened_on"]
    if (pod.get("entry_evidence") or {}).get("mode") == "historical_replay":
        # The paper account starts in cash before the first session opens.
        anchor = db.query_one(f"SELECT max(trade_date) AS day FROM {db.qualified('trade_calendar')} WHERE trade_date < %s",
                              [benchmark_start])
        if not anchor["day"]:
            raise ValueError("缺少回放期初前一交易日")
        benchmark_start = anchor["day"]
    benchmark_return = benchmark_cum_return(pod["benchmark_code"], benchmark_start, trade_date)
    excess = (unit_nav - 1.0) - benchmark_return if benchmark_return is not None else None

    db.upsert(
        "pod_nav",
        [
            "pod_id", "trade_date", "total_value", "cash", "market_value", "unit_nav",
            "daily_return", "cum_return", "drawdown", "benchmark_return", "excess_return",
        ],
        [
            (
                pod["pod_id"], trade_date, round(total, 2), round(account.cash, 2),
                round(total - account.cash, 2), round(unit_nav, 6), None if daily_return is None else round(daily_return, 6),
                round(unit_nav - 1.0, 6), round(drawdown, 6),
                None if benchmark_return is None else round(benchmark_return, 6),
                None if excess is None else round(excess, 6),
            )
        ],
        conflict=["pod_id", "trade_date"],
    )

    mapping = instrument_map(include_inactive=True)
    position_rows = []
    for code in account.lots:
        price = prices.get(code)
        if price is None:
            continue
        shares = account.shares_of(code)
        if shares <= 1e-9:  # 已清仓的标的不要留 0 权重脏行
            continue
        value = account.value_of(code, price)
        weight = value / total if total > 0 else 0.0
        position_rows.append(
            (
                pod["pod_id"],
                trade_date,
                mapping[code]["instrument_id"],
                round(weight, 6),
                round(account.shares_of(code), 6),
                round(value, 2),
                round(account.average_cost(code), 6),
            )
        )
    db.execute(
        f"DELETE FROM {db.qualified('pod_position')} WHERE pod_id = %s AND trade_date = %s",
        [pod["pod_id"], trade_date],
    )
    if position_rows:
        db.execute_many(
            f"INSERT INTO {db.qualified('pod_position')} "
            "(pod_id, trade_date, instrument_id, weight, shares, market_value, avg_cost) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            position_rows,
        )

    return {
        "total_value": total,
        "unit_nav": unit_nav,
        "daily_return": daily_return,
        "cum_return": unit_nav - 1.0,
        "drawdown": drawdown,
        "benchmark_return": benchmark_return,
        "excess_return": excess,
        "cash": account.cash,
        "market_value": total - account.cash,
    }


def nav_history(pod_id: int) -> pd.DataFrame:
    frame = db.query_df(
        f"SELECT * FROM {db.qualified('pod_nav')} WHERE pod_id = %s ORDER BY trade_date",
        [pod_id],
    )
    if not frame.empty:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.set_index("trade_date")
    return frame


def positions(pod_id: int, trade_date: date | None = None) -> pd.DataFrame:
    sql = [
        "SELECT p.trade_date, i.code, i.name, p.weight, p.market_value, p.shares",
        f"FROM {db.qualified('pod_position')} p",
        f"JOIN {db.qualified('instrument')} i USING (instrument_id)",
        "WHERE p.pod_id = %s",
    ]
    params: list[object] = [pod_id]
    if trade_date:
        sql.append("AND p.trade_date = %s")
        params.append(trade_date)
    else:
        sql.append("AND p.trade_date = (SELECT max(trade_date) FROM " + db.qualified("pod_nav") + " WHERE pod_id = %s)")
        params.append(pod_id)
    sql.append("ORDER BY p.weight DESC")
    return db.query_df(" ".join(sql), params)


def pod_boards(pod_id: int, trade_date: date | None = None) -> list[str]:
    """该仓当前持仓涉及的板块名（用于开新仓时的重合度检查）。"""
    positions_frame = positions(pod_id, trade_date)
    if positions_frame.empty:
        return []
    codes = list(positions_frame["code"])
    rows = db.query(
        f"""
        SELECT DISTINCT b.name
        FROM {db.qualified('instrument_board')} ib
        JOIN {db.qualified('board')} b USING (board_id)
        JOIN {db.qualified('instrument')} i USING (instrument_id)
        WHERE i.code = ANY(%s)
        """,
        [codes],
    )
    return [row["name"] for row in rows]
