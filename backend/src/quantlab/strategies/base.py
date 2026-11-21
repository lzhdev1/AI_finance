from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    """策略只负责输出目标权重，不碰资金、不碰下单。"""

    name: str = "base"

    @abstractmethod
    def target_weights(self, nav: pd.DataFrame) -> dict[str, float]:
        """nav 只包含决策时点可获得的数据（最后一行为 as_of），禁止使用未来数据。"""

    def describe(self) -> dict[str, object]:
        return {"name": self.name}