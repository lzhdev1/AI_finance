from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.getenv("QUANTLAB_CONFIG_DIR", ROOT / "configs"))
DATA_DIR = Path(os.getenv("QUANTLAB_DATA_DIR", ROOT / "data"))
LOG_DIR = Path(os.getenv("QUANTLAB_LOG_DIR", ROOT / "logs"))
DB_DIR = Path(os.getenv("QUANTLAB_DB_DIR", ROOT / "db"))


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _read_yaml(path: Path | str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class AgentConfig:
    """仓的生命周期规则。建仓时写死，运行期只读。"""

    max_pods: int = 5
    min_pods: int = 1
    pod_capital: float = 100_000.0
    pod_min_eval_days: int = 20
    pod_hard_stop_drawdown: float = 0.12
    pod_soft_retire_hit_rate: float = 0.45
    pod_cooldown_days: int = 10
    open_max_board_overlap: float = 0.50
    pod_min_interval_days: int = 5
    max_position: float = 0.35
    max_board_exposure: float = 0.45
    max_monthly_turnover: float = 0.60
    decision_cutoff: str = "14:00"
    auto_open_pod: bool = True


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = True
    model_fast: str = "deepseek-chat"
    model_deep: str = "deepseek-reasoner"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: int = 120
    json_mode: bool = True


@dataclass(frozen=True)
class Settings:
    timezone: str
    data_dir: Path
    log_dir: Path
    snapshot_dir: Path
    db_schema: str
    statement_timeout_ms: int
    schedule: dict[str, str]
    benchmark: str
    board_kinds: list[str]
    news_lookback_days: int
    news_sources: list[str]
    backtest_start: str
    backtest_end: str | None
    initial_cash: float
    min_trade_amount: float
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    raw = _read_yaml(CONFIG_DIR / "settings.yaml")
    app = raw.get("app", {})
    db = raw.get("database", {})
    market = raw.get("market", {})
    news = market.get("news", {})
    bt = raw.get("backtest", {})
    agent = AgentConfig(**raw.get("agent", {}))
    llm_raw = raw.get("llm", {})
    llm = LLMConfig(
        enabled=bool(llm_raw.get("enabled", True)) and bool(os.getenv("LLM_API_KEY", "").strip()),
        model_fast=str(llm_raw.get("model_fast", "deepseek-chat")),
        model_deep=str(llm_raw.get("model_deep", "deepseek-reasoner")),
        temperature=float(os.getenv("LLM_TEMPERATURE", llm_raw.get("temperature", 0.2))),
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
        timeout=int(llm_raw.get("timeout", 120)),
        json_mode=bool(llm_raw.get("json_mode", True)),
    )
    return Settings(
        timezone=str(app.get("timezone", "Asia/Shanghai")),
        data_dir=_resolve(app.get("data_dir", "data")),
        log_dir=_resolve(app.get("log_dir", "logs")),
        snapshot_dir=_resolve(app.get("snapshot_dir", "data/snapshots")),
        db_schema=str(db.get("schema", "quantlab")),
        statement_timeout_ms=int(db.get("statement_timeout_ms", 120_000)),
        schedule={k: str(v) for k, v in raw.get("schedule", {}).items()},
        benchmark=str(market.get("benchmark", "000300")),
        board_kinds=list(market.get("board_kinds", ["industry", "concept"])),
        news_lookback_days=int(news.get("lookback_days", 3)),
        news_sources=([s.strip() for s in os.environ["NEWS_SOURCES"].split(",") if s.strip()]
                      if os.getenv("NEWS_SOURCES", "").strip()
                      else list(news.get("sources", ["cls", "eastmoney"]))),
        backtest_start=str(bt.get("start", "2020-01-01")),
        backtest_end=str(bt["end"]) if bt.get("end") else None,
        initial_cash=float(bt.get("initial_cash", 100_000)),
        min_trade_amount=float(bt.get("min_trade_amount", 200)),
        agent=agent,
        llm=llm,
    )


def load_universe(path: Path | str | None = None) -> dict[str, Any]:
    return _read_yaml(Path(path) if path else CONFIG_DIR / "universe.yaml")


def load_portfolio(path: Path | str | None = None) -> dict[str, Any]:
    return _read_yaml(Path(path) if path else CONFIG_DIR / "portfolio.yaml")


def universe_codes(universe: dict[str, Any]) -> list[str]:
    return [asset["code"] for asset in universe["assets"]]
