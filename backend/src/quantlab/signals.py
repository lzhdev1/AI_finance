from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from .calendar import signal_cutoff_note
from .config import Settings, load_settings, universe_codes
from .data import load_nav
from .strategies.dual_momentum import DualMomentum


@dataclass
class Order:
    code: str
    name: str
    side: str
    amount: float
    current_value: float
    target_value: float
    target_weight: float


@dataclass
class SignalReport:
    as_of: str
    total_value: float
    cash: float
    targets: dict[str, float]
    names: dict[str, str]
    orders: list[Order] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"## 调仓信号 · {self.as_of}",
            "",
            f"总资产：**¥{self.total_value:,.0f}**（现金 ¥{self.cash:,.0f}）",
            "",
        ]
        if not self.orders:
            lines.append("**无需调仓。**")
        else:
            lines += [
                "| 操作 | 基金 | 代码 | 当前市值 | 目标市值 | 金额 |",
                "|---|---|---|---:|---:|---:|",
            ]
            for order in self.orders:
                lines.append(
                    f"| {order.side} | {order.name} | {order.code} | "
                    f"¥{order.current_value:,.0f} | ¥{order.target_value:,.0f} | "
                    f"¥{order.amount:,.0f} |"
                )
        lines += ["", "目标权重：", ""]
        for code, weight in sorted(self.targets.items(), key=lambda item: -item[1]):
            if weight > 0:
                lines.append(f"- {self.names.get(code, code)}（{code}）：{weight:.1%}")
        lines += ["", "---", ""]
        lines += [f"> {note}" for note in self.notes]
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False, indent=2)


def build_strategy(
    universe: dict,
    lookback: int = 60,
    top_n: int = 2,
    use_absolute: bool = True,
) -> DualMomentum:
    return DualMomentum(
        defensive=universe["defensive"],
        lookback=lookback,
        top_n=top_n,
        use_absolute=use_absolute,
    )


def generate_signal(
    universe: dict,
    portfolio: dict,
    strategy: DualMomentum,
    nav: pd.DataFrame | None = None,
    settings: Settings | None = None,
) -> SignalReport:
    settings = settings or load_settings()
    codes = universe_codes(universe)
    names = {asset["code"]: asset["name"] for asset in universe["assets"]}

    if nav is None or nav.empty:
        nav = load_nav(codes)
    nav = nav.dropna(how="all").sort_index()
    if nav.empty:
        raise RuntimeError("数据库中没有净值，先运行 scripts/backfill.py")

    as_of = nav.index[-1]
    targets = strategy.target_weights(nav)
    targets = {code: float(targets.get(code, 0.0)) for code in codes}

    holdings_raw = portfolio.get("holdings", {}) or {}
    holdings = {code: float(holdings_raw.get(code, 0.0) or 0.0) for code in codes}
    cash = float(portfolio.get("cash", 0.0) or 0.0)
    total_value = sum(holdings.values()) + cash
    if total_value <= 0:
        raise ValueError("持仓和现金都为 0，请先更新 configs/portfolio.yaml")

    notes = [
        signal_cutoff_note(settings),
        "费率按 C 类估算时，持有满 7/30 天后赎回费为 0；请以基金合同为准。",
    ]
    portfolio_as_of = portfolio.get("as_of")
    if portfolio_as_of:
        lag = abs((pd.Timestamp(as_of) - pd.Timestamp(str(portfolio_as_of))).days)
        if lag > 5:
            notes.append(f"持仓快照日期为 {portfolio_as_of}，与最新净值相差 {lag} 天，请先更新持仓。")

    orders: list[Order] = []
    for code in codes:
        current = holdings[code]
        target = total_value * targets[code]
        delta = target - current
        if abs(delta) < settings.min_trade_amount:
            continue
        orders.append(
            Order(
                code=code,
                name=names.get(code, code),
                side="买入" if delta > 0 else "卖出",
                amount=round(abs(delta), 2),
                current_value=round(current, 2),
                target_value=round(target, 2),
                target_weight=round(targets[code], 4),
            )
        )

    # 先卖后买，先腾出资金
    orders.sort(key=lambda order: (order.side == "买入", -order.amount))
    return SignalReport(
        as_of=as_of.strftime("%Y-%m-%d"),
        total_value=round(total_value, 2),
        cash=round(cash, 2),
        targets=targets,
        names=names,
        orders=orders,
        notes=notes,
    )


def save_signal(report: SignalReport, settings: Settings | None = None) -> tuple[Path, Path]:
    settings = settings or load_settings()
    signal_dir = settings.data_dir / "signals"
    signal_dir.mkdir(parents=True, exist_ok=True)
    stem = report.as_of.replace("-", "")
    md_path = signal_dir / f"{stem}.md"
    json_path = signal_dir / f"{stem}.json"
    md_path.write_text(report.to_markdown(), encoding="utf-8")
    json_path.write_text(report.to_json(), encoding="utf-8")
    return md_path, json_path


def latest_signal(settings: Settings | None = None) -> SignalReport | None:
    settings = settings or load_settings()
    candidates = sorted((settings.data_dir / "signals").glob("*.json"))
    if not candidates:
        return None
    payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    orders = [Order(**item) for item in payload.pop("orders", [])]
    return SignalReport(orders=orders, **payload)
