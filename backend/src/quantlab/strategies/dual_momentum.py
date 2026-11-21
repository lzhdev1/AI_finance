from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .base import Strategy


@dataclass
class DualMomentum(Strategy):
    """双动量：绝对动量过滤 + 相对动量排序，空仓时切防御资产。"""

    defensive: str
    lookback: int = 60
    top_n: int = 2
    use_absolute: bool = True
    name: str = "dual_momentum"

    def target_weights(self, nav: pd.DataFrame) -> dict[str, float]:
        codes = list(nav.columns)
        weights = {code: 0.0 for code in codes}
        if self.defensive not in weights:
            weights[self.defensive] = 0.0

        clean = nav.dropna(how="all").sort_index()
        if len(clean) <= self.lookback:
            weights[self.defensive] = 1.0
            return weights

        latest = clean.iloc[-1]
        base = clean.iloc[-1 - self.lookback]
        momentum = (latest / base - 1.0).dropna()

        if self.defensive in momentum:
            defensive_momentum = float(momentum[self.defensive])
        else:
            defensive_momentum = 0.0

        candidates = [c for c in momentum.index if c != self.defensive]
        candidates.sort(key=lambda c: float(momentum[c]), reverse=True)

        selected: list[str] = []
        for code in candidates:
            value = float(momentum[code])
            if value <= 0:
                continue
            if self.use_absolute and value <= defensive_momentum:
                continue
            selected.append(code)
            if len(selected) >= self.top_n:
                break

        if not selected:
            weights[self.defensive] = 1.0
            return weights

        weight = 1.0 / len(selected)
        for code in selected:
            weights[code] = weight
        return weights

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "defensive": self.defensive,
            "lookback": self.lookback,
            "top_n": self.top_n,
            "use_absolute": self.use_absolute,
        }