"""历史策略诊断：板块因子 IC、方向概率校准与权重建议。"""

from __future__ import annotations

import logging
from datetime import date
from itertools import pairwise

import pandas as pd

from . import db, factors

log = logging.getLogger(__name__)

MIN_IC_SAMPLES = 40  # 因子 IC 至少要这么多天才有意义
MIN_CALIBRATION_SAMPLES = 50  # 概率校准至少要这么多条已结算预测
SHRINK = 0.5  # 权重调整幅度：只走一半，留一半给"我不知道"


# ---------------------------------------------------------------- 因子 IC


def board_panel(days: int = 240, as_of: date | None = None) -> dict[str, pd.DataFrame]:
    """板块行情宽表（收盘价、成交额、主力净流入），用于逐日重算因子。"""
    rows = db.query(
        f"""
        SELECT q.trade_date, b.name AS board, q.close, q.amount, f.main_net
        FROM {db.qualified("board_quote")} q
        JOIN {db.qualified("board")} b USING (board_id)
        LEFT JOIN {db.qualified("board_fund_flow")} f
               ON f.board_id = q.board_id AND f.trade_date = q.trade_date AND f.period='1d'
        WHERE q.trade_date BETWEEN %s::date - %s::int AND %s::date
        ORDER BY q.trade_date
        """,
        [as_of or date.today(), days, as_of or date.today()],
    )
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    out = {}
    for column in ("close", "amount", "main_net"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        out[column] = frame.pivot_table(
            index="trade_date", columns="board", values=column, aggfunc="mean"
        )
    return out


def factor_ic(
    days: int = 60, min_history: int = factors.MIN_HISTORY, as_of: date | None = None
) -> pd.DataFrame:
    """逐日计算各因子与"次日板块收益"的横截面秩相关（Rank IC）。

    IC > 0 表示这个因子当天的排序对未来一天有正向指引；ICIR = 均值/标准差，
    衡量稳定性。ICIR 低于 0.2 基本等于噪声——再把它当信号用就是自欺欺人。
    """
    panel = board_panel(days=max(days + 60, 240), as_of=as_of)
    if not panel:
        return pd.DataFrame()

    close, amount, flow = panel["close"], panel["amount"], panel["main_net"]
    dates = [d for d in close.index if close.loc[:d, :].notna().sum().max() >= min_history]
    if len(dates) < 2:
        return pd.DataFrame()

    records = []
    for index in range(max(0, len(dates) - days - 1), len(dates) - 1):
        today, tomorrow = dates[index], dates[index + 1]
        window = close.loc[:today]
        if len(window) < min_history:
            continue

        mom5 = window.iloc[-1] / window.iloc[-6] - 1.0 if len(window) > 6 else None
        mom20 = window.iloc[-1] / window.iloc[-21] - 1.0 if len(window) > 21 else None
        if mom5 is None or mom20 is None:
            continue

        avg_amount = amount.loc[:today].tail(60).mean()
        flow_window = flow.loc[:today].tail(5).sum(min_count=1)
        flow_ratio = (flow_window / avg_amount.replace(0, pd.NA)).astype(float)

        forward = (close.loc[tomorrow] / close.loc[today] - 1.0).dropna()
        row = {"trade_date": today, "samples": 0}
        for name, series in (
            ("momentum_5", mom5),
            ("momentum_20", mom20),
            ("flow_ratio", flow_ratio),
        ):
            aligned = pd.concat([series, forward], axis=1, keys=["factor", "forward"]).dropna()
            if len(aligned) < 10:
                row[name] = None
                continue
            row[name] = float(aligned["factor"].rank().corr(aligned["forward"].rank()))
            row["samples"] = len(aligned)
        records.append(row)

    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records).set_index("trade_date")
    stats = []
    for name in ("momentum_5", "momentum_20", "flow_ratio"):
        series = frame[name].dropna()
        if len(series) == 0:
            continue
        mean = float(series.mean())
        std = float(series.std(ddof=1)) if len(series) > 1 else 0.0
        stats.append(
            {
                "factor": name,
                "ic_mean": mean,
                "ic_std": std,
                "icir": mean / std if std else 0.0,
                "positive_rate": float((series > 0).mean()),
                "days": len(series),
            }
        )
    return pd.DataFrame(stats).sort_values("ic_mean", ascending=False) if stats else pd.DataFrame()


def save_factor_ic(trade_date: date | None = None) -> int:
    """把当天算出的 IC 落库，形成可回溯的历史序列。"""
    stats = factor_ic(as_of=trade_date)
    if stats.empty:
        return 0
    trade_date = trade_date or date.today()
    rows = [
        (trade_date, row.factor, row.ic_mean, row.ic_std, row.icir, row.positive_rate, row.days)
        for row in stats.itertuples(index=False)
    ]
    db.execute_many(
        f"""
        INSERT INTO {db.qualified("factor_ic")}
            (trade_date, factor, ic_mean, ic_std, icir, positive_rate, samples)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trade_date, factor) DO UPDATE
          SET ic_mean = EXCLUDED.ic_mean, ic_std = EXCLUDED.ic_std,
              icir = EXCLUDED.icir, positive_rate = EXCLUDED.positive_rate,
              samples = EXCLUDED.samples
        """,
        rows,
    )
    return len(rows)


def suggested_weights(stats: pd.DataFrame | None = None) -> dict[str, float]:
    """按 IC 调整因子权重的**建议**（不自动生效，需要人确认）。

    规则：只保留 IC 为正的因子，按 IC 比例分配，并与原权重的 50/50 混合。
    样本不足或全部为负时，原样返回——宁可不改，也不要把权重调成噪声。
    """
    base = dict(factors.WEIGHTS)
    if stats is None:
        stats = factor_ic()
    if stats.empty or int(stats["days"].max()) < MIN_IC_SAMPLES:
        return base

    usable = stats[(stats["ic_mean"] > 0) & (stats["days"] >= MIN_IC_SAMPLES)]
    if usable.empty:
        return base
    total = float(usable["ic_mean"].sum())
    learned = {row.factor: float(row.ic_mean) / total for row in usable.itertuples(index=False)}

    merged = {}
    for name, original in base.items():
        merged[name] = (1 - SHRINK) * original + SHRINK * learned.get(name, 0.0)
    norm = sum(merged.values()) or 1.0
    return {name: value / norm for name, value in merged.items()}


# ---------------------------------------------------------------- 概率校准


def calibration() -> dict:
    """按较早交易日拟合等频分箱，使用随后交易日评估真实上涨事件的 Brier。"""
    frame = db.query_df(
        f"SELECT trade_date, prob_up, (realized_return > 0)::int AS up FROM {db.qualified('pod_prediction')} "
        "WHERE scored_at IS NOT NULL AND realized_return IS NOT NULL AND prob_up IS NOT NULL "
        "AND target='pod_nav' AND horizon_days=1 ORDER BY trade_date, prediction_id"
    )
    result = {"bins": [], "samples": len(frame), "enabled": False}
    days = sorted(frame["trade_date"].unique()) if not frame.empty else []
    if len(frame) < MIN_CALIBRATION_SAMPLES or len(days) < 5:
        result["reason"] = (
            f"已结算 {len(frame)} 条，需要至少 {MIN_CALIBRATION_SAMPLES} 条且涵盖五个交易日。"
        )
        return result
    split = days[min(len(days) - 1, int(0.8 * len(days)))]
    train, test = frame[frame.trade_date < split].copy(), frame[frame.trade_date >= split].copy()
    groups = min(5, max(2, len(train) // 20))
    # Only training outcomes determine bin boundaries and estimated win rates.
    breaks = sorted(
        {float(x) for x in train.prob_up.quantile([i / groups for i in range(1, groups)])}
    )
    bounds = [0.0, *[x for x in breaks if 0.0 < x < 1.0], 1.0]
    prior = float(train.up.mean())
    prior_strength = 5
    for lo, hi in pairwise(bounds):
        group = train[
            (train.prob_up <= hi) & ((train.prob_up > lo) | ((lo == 0) & (train.prob_up == 0)))
        ]
        actual = float((group.up.sum() + prior_strength * prior) / (len(group) + prior_strength))
        result["bins"].append(
            {
                "range": f"{lo:.3f}–{hi:.3f}",
                "lower": lo,
                "upper": hi,
                "actual": actual,
                "predicted": float(group.prob_up.mean()) if len(group) else (lo + hi) / 2,
                "samples": len(group),
            }
        )
    calibrated = test.prob_up.map(lambda p: _map_probability(float(p), result["bins"]))
    result.update(
        enabled=True,
        validation_samples=len(test),
        brier_raw=float(((test.prob_up - test.up) ** 2).mean()),
        brier_calibrated=float(((calibrated - test.up) ** 2).mean()),
    )
    return result


def _map_probability(prob: float, bins: list[dict]) -> float:
    for item in bins:
        if prob <= item["upper"]:
            return item["actual"]
    return prob


def apply_calibration(prob: float) -> float:
    table = calibration()
    return _map_probability(prob, table["bins"]) if table["enabled"] else prob


# ---------------------------------------------------------------- 自检


def learning_report() -> dict:
    """给页面用的一份汇总：现在到底"学到"了什么。"""
    stats = factor_ic()
    calib = calibration()
    report = {
        "factor_stats": stats.to_dict("records") if not stats.empty else [],
        "weights_now": dict(factors.WEIGHTS),
        "weights_suggested": suggested_weights(stats),
        "calibration": calib,
        "ic_samples": int(stats["days"].max()) if not stats.empty else 0,
        "ready_for_weight_change": bool(
            not stats.empty and int(stats["days"].max()) >= MIN_IC_SAMPLES
        ),
    }
    return report
