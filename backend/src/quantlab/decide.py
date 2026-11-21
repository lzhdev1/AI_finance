"""每日决策层：把"数据 + 因子"变成"动作 + 决策卡"。

一条铁律：**这里产出的每句话，都必须能指向一个数据点**。
所以卡片正文是模板渲染的，不是自由发挥的——LLM 只被允许在 ``narrative`` 字段里
做措辞润色，不允许它改变仓位数字。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from psycopg.types.json import Jsonb

from . import db, factors, pod_store
from .config import load_settings
from .data import load_board_flows, load_board_quotes, load_nav, load_news
from .pods import clamp_targets
from .strategies.dual_momentum import DualMomentum

log = logging.getLogger(__name__)

# 这些板块名太泛，出现在新闻里多半是巧合，统计"新闻热度"时排除，避免噪声
GENERIC_BOARDS = {"综合", "其他电子", "其他社会服务", "其他电源设备", "贸易", "教育", "多元金融"}


# ---------------------------------------------------------------- 市场快照

@dataclass
class MarketView:
    trade_date: date
    nav: pd.DataFrame                      # 基金净值宽表（截至 trade_date 或更早）
    prices: dict[str, float]               # 用于估值的净值
    board_scores: pd.DataFrame             # 板块打分（可能为空）
    market_flow: float | None = None       # 当日大盘主力净流入（亿元）
    news_counts: pd.Series | None = None   # 板块 → 近 3 日相关新闻条数
    news_samples: dict[str, list[str]] = field(default_factory=dict)
    intraday: dict[str, float] = field(default_factory=dict)   # 板块盘中实时涨跌（%）
    intraday_ts: str | None = None
    price_as_of: date | None = None        # 净值实际截至哪一天
    notes: list[str] = field(default_factory=list)

    @property
    def has_boards(self) -> bool:
        return not self.board_scores.empty

    @property
    def is_intraday(self) -> bool:
        """净值还没出来、只能靠盘中行情判断——14:30 决策时就是这个状态。"""
        return self.price_as_of is not None and self.price_as_of < self.trade_date


def board_news_terms(
    day: date, board_names: Sequence[str], window_days: int = 3, per_board: int = 3, *, cutoff=None
) -> tuple[pd.Series, dict[str, list[str]]]:
    """统计每个板块在近 N 日新闻里被提到多少次，并留下标题作为证据。

    只做"板块名出现在标题或正文里"的字面匹配——粗糙，但完全可复核：
    决策卡里给出的每条新闻标题，你都能自己点开看对不对。
    """
    start = (day - timedelta(days=window_days)).strftime("%Y-%m-%d")
    news = load_news(start=start, end=day.strftime("%Y-%m-%d"), limit=800, cutoff=cutoff)
    if news.empty:
        return pd.Series(dtype=float), {}

    titles = news["title"].fillna("").astype(str).tolist()
    bodies = news["body"].fillna("").astype(str).tolist()
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for name in board_names:
        if len(name) < 2 or name in GENERIC_BOARDS:
            continue
        in_title = [titles[i] for i in range(len(titles)) if name in titles[i]]
        # 标题命中更能说明"这条新闻真的在讲这个板块"，正文命中多半是顺带一提
        in_body = [
            titles[i] for i in range(len(titles))
            if name not in titles[i] and name in bodies[i]
        ]
        total = len(in_title) + len(in_body)
        if total:
            counts[name] = total
            samples[name] = (in_title + in_body)[:per_board]
    return pd.Series(counts, dtype=float), samples


def build_market_view(
    trade_date: date,
    lookback_days: int = 200,
    as_of: date | None = None,
    board_end: date | None = None,
) -> MarketView:
    """构造决策时点的市场视图。

    ``trade_date`` 决定"能用哪些行情"（不能超过它，否则就是未来函数）；
    ``board_end`` 是板块因子的数据截止日——14:30 决策时必须传前一交易日；
    ``as_of`` 决定"新闻能看到哪一天"——实盘是今天（盘后决策时当天新闻已经可见），
    历史回放必须等于回放日，否则会偷看明天的消息。
    """
    as_of = min(as_of or trade_date, trade_date)
    # 用「所有启用中的基金」而不是 universe 列表：仓里可能还持有已从配置里移出的老基金，
    # 只用 universe 会导致这些持仓被当成 0 元估值。
    codes = list(pod_store.instrument_map().keys())
    nav = load_nav(codes, end=str(board_end or trade_date))
    nav = nav.dropna(how="all").sort_index()
    if nav.empty:
        raise RuntimeError("数据库里没有净值，先跑 scripts/ingest.py funds")

    latest_day = nav.index[-1]
    prices = {
        code: float(value)
        for code, value in nav.iloc[-1].items()
        if not pd.isna(value)
    }

    notes: list[str] = []
    # 板块因子只能用「决策时点已经收盘」的数据：
    # 14:30 决策时今天的板块行情还没出来，用 T 日收盘数据就是未来函数。
    cutoff = None
    if board_end and board_end < trade_date:
        cutoff = pd.Timestamp(f"{trade_date} {load_settings().agent.decision_cutoff}", tz="Asia/Shanghai")
    board_end = board_end or latest_day.date()
    end = board_end.strftime("%Y-%m-%d")
    start = (board_end - pd.Timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    quotes = load_board_quotes(start=start, end=end)
    flows = load_board_flows(start=start, end=end)
    amounts = load_board_quotes(start=start, end=end, column="amount")

    scores = pd.DataFrame()
    mentions = pd.Series(dtype=float)
    samples: dict[str, list[str]] = {}
    if quotes.empty:
        notes.append("板块行情为空，本轮退化为基金级动量")
    else:
        mentions, samples = board_news_terms(as_of, quotes.columns, cutoff=cutoff)
        scores = factors.board_scores(quotes, amounts, flows, news_counts=mentions)
        if scores.empty:
            notes.append("板块样本不足，本轮退化为基金级动量")

    market_flow = None
    row = db.query_one(
        f"SELECT main_net FROM {db.qualified('market_fund_flow')} WHERE trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT 1",
        [board_end],
    )
    if row and row["main_net"] is not None:
        market_flow = float(row["main_net"]) / 1e8

    # 盘中实时涨跌：决策时点的"现在几点了"——只用于解释和 30 分钟预测，不参与打分
    intraday, intraday_ts = {}, None
    snap = db.query_one(
        f"SELECT max(ts) AS ts FROM {db.qualified('board_intraday')} "
        "WHERE ts::date = %s AND ts <= %s",
        [trade_date, cutoff or pd.Timestamp(f"{trade_date} 23:59:59", tz="Asia/Shanghai")],
    )
    if snap and snap["ts"]:
        rows = db.query(
            f"SELECT board_name, pct_chg FROM {db.qualified('board_intraday')} WHERE ts = %s",
            [snap["ts"]],
        )
        intraday = {item["board_name"]: float(item["pct_chg"]) for item in rows}
        intraday_ts = snap["ts"].strftime("%H:%M")
    if intraday:
        notes.append(f"盘中行情 {intraday_ts} 快照，覆盖 {len(intraday)} 个板块")

    return MarketView(
        trade_date=trade_date,
        nav=nav,
        prices=prices,
        board_scores=scores,
        market_flow=market_flow,
        news_counts=mentions,
        news_samples=samples,
        intraday=intraday,
        intraday_ts=intraday_ts,
        price_as_of=latest_day.date(),
        notes=notes,
    )


# ---------------------------------------------------------------- 策略

def strategy_defensive(market: MarketView, universe: dict, incumbent: Mapping[str, float] | None = None) -> tuple[dict[str, float], str]:
    defensive = universe["defensive"]
    return {defensive: 1.0}, "防御仓：始终持有短债，作为所有进攻仓的对照组"


def strategy_fund_momentum(
    market: MarketView,
    universe: dict,
    incumbent: Mapping[str, float] | None = None,
    lookback: int = 60,
    margin: float = 0.02,
) -> tuple[dict[str, float], str]:
    cap = load_settings().agent.max_position
    # 持仓数至少要够"配满"：单只上限 35% 时至少要 3 只，否则天然留现金
    top_n = max(2, math.ceil(1.0 / cap) if cap > 0 else 2)
    strategy = DualMomentum(defensive=universe["defensive"], lookback=lookback, top_n=top_n)
    weights = strategy.target_weights(market.nav)
    note = ""

    # 滞回：新组合的平均动量必须比现有组合高出 margin 才换仓。
    # 没有这一条，排名前两名每天互相反超，就会变成每天全仓来回申赎。
    if incumbent:
        held = {code for code, weight in incumbent.items() if weight > 0.001}
        fresh = {code for code, weight in weights.items() if weight > 0.001}
        if held and held != fresh:
            gain = _avg_momentum(market.nav, fresh, lookback) - _avg_momentum(market.nav, held, lookback)
            if gain < margin:
                kept = {code: incumbent[code] for code in held if code in market.nav.columns}
                total = sum(kept.values())
                kept = {code: value / total for code, value in kept.items()} if total > 0 else weights
                note = f"（新组合动量仅高 {gain:+.2%}，未达换仓门槛 {margin:.0%}，维持原持仓）"
                return kept, f"基金级双动量（{lookback} 日）：维持不动{note}"

    named = ", ".join(f"{code} {weight:.0%}" for code, weight in weights.items() if weight > 0)
    return weights, f"基金级双动量（{lookback} 日，取前 {top_n} 名）：{named or '全部空仓→防御'}"


def _avg_momentum(nav: pd.DataFrame, codes: set[str], lookback: int) -> float:
    usable = [code for code in codes if code in nav.columns]
    if not usable or len(nav) <= lookback:
        return 0.0
    latest = nav.iloc[-1]
    past = nav.iloc[-1 - lookback]
    values = [latest[code] / past[code] - 1.0 for code in usable if past[code] == past[code] and past[code]]
    return float(sum(values) / len(values)) if values else 0.0


def strategy_momentum_flow(
    market: MarketView,
    universe: dict,
    incumbent: Mapping[str, float] | None = None,
    top_boards: int = 2,
) -> tuple[dict[str, float], str]:
    """板块资金流 + 动量：选强势板块，再落到跟踪该板块的基金上。

    没有板块数据、或板块没有对应可投基金时，退化为基金级动量。
    """
    if not market.has_boards:
        return strategy_fund_momentum(market, universe, incumbent)

    # 只在"有可投基金"的板块里挑：没有对应基金的板块，选中了也执行不了
    mapping = board_fund_map()
    tradable = [board for board in market.board_scores.index if mapping.get(board)]
    if not tradable:
        weights, _ = strategy_fund_momentum(market, universe, incumbent)
        return weights, "板块均无可投基金（先跑 scripts/link_boards.py），退化为基金级动量"

    scores = market.board_scores.loc[tradable]
    picks = factors.pick_boards(scores, top_n=top_boards)
    if not picks:
        weights, _ = strategy_fund_momentum(market, universe, incumbent)
        return weights, "板块全线走弱（无正分板块），切换到基金级动量/防御"

    per_board = 1.0 / len(picks)
    weights: dict[str, float] = {}
    parts = []
    for board in picks:
        codes_for_board = mapping[board]
        # 同一板块有多只基金时平均分配
        share = per_board / len(codes_for_board)
        for code in codes_for_board:
            weights[code] = weights.get(code, 0.0) + share
        score = scores.loc[board]
        parts.append(
            f"{board}（综合分 {score['score']:+.2f}，5日动量 {score['momentum_5']:+.1%}，"
            f"资金流强度 {score['flow_ratio']:+.1%}）→ {'/'.join(codes_for_board)}"
        )
    return weights, "板块资金流轮动：" + "；".join(parts)


def strategy_momentum_flow_broad(
    market: MarketView,
    universe: dict,
    incumbent: Mapping[str, float] | None = None,
) -> tuple[dict[str, float], str]:
    """分散版：取前 3 个板块。与集中版同时存在才有意义——它们是两种不同的赌法。"""
    return strategy_momentum_flow(market, universe, incumbent, top_boards=3)


def board_fund_map() -> dict[str, list[str]]:
    """板块名 → 可投基金代码列表（只含已启用且有净值数据的基金）。"""
    nav_table = db.qualified("instrument_nav")
    rows = db.query(
        f"""
        SELECT b.name AS board, i.code
        FROM {db.qualified('instrument_board')} ib
        JOIN {db.qualified('board')} b USING (board_id)
        JOIN {db.qualified('instrument')} i USING (instrument_id)
        WHERE i.is_active
          AND EXISTS (SELECT 1 FROM {nav_table} n WHERE n.instrument_id = i.instrument_id)
        ORDER BY b.name, i.code
        """
    )
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["board"], []).append(row["code"])
    return mapping


STRATEGIES = {
    "defensive": strategy_defensive,
    "fund_momentum": strategy_fund_momentum,
    "momentum_flow": strategy_momentum_flow,
    "momentum_flow_broad": strategy_momentum_flow_broad,
}


def defensive_codes() -> set[str]:
    rows = db.query(f"SELECT code FROM {db.qualified('instrument')} WHERE role = 'defensive'")
    return {row["code"] for row in rows}


def market_risk_off(trade_date: date, code: str, window: int = 20) -> tuple[bool, str]:
    """基准指数是否跌破 20 日均线 —— 这是"提前跑路"的闸门。

    不做花哨的判断：跌破均线就把股票类敞口砍半，站回去再加回来。
    比"感觉要跌了"可复核，也比纯动量慢半拍但不会追高追在半山腰。
    """
    rows = db.query(
        f"SELECT trade_date, close FROM {db.qualified('index_quote')} "
        "WHERE index_code = %s AND trade_date <= %s ORDER BY trade_date DESC LIMIT %s",
        [code, trade_date, window + 5],
    )
    if len(rows) < window + 1:
        return False, ""
    closes = [float(row["close"]) for row in rows][:window]
    latest = closes[0]
    average = sum(closes) / len(closes)
    if latest < average:
        return True, (
            f"基准 {code} 收于 {latest:.0f}，跌破 {window} 日均线 {average:.0f}，"
            f"股票类敞口减半"
        )
    return False, ""


def targets_for(
    pod: Mapping,
    market: MarketView,
    universe: dict,
    incumbent: Mapping[str, float] | None = None,
) -> tuple[dict[str, float], str, float]:
    """返回（目标权重, 决策依据, 目标总仓位）。总仓位 < 1 表示主动留现金。"""
    func = STRATEGIES.get(pod["strategy_key"])
    if func is None:
        raise ValueError(f"未知策略 {pod['strategy_key']}")
    raw, reason = func(market, universe, incumbent)

    exposure = 1.0
    if pod["strategy_key"] != "defensive":
        risk_off, note = market_risk_off(market.price_as_of or market.trade_date, pod["benchmark_code"])
        if risk_off:
            exposure = 0.5
            reason = f"{reason}；{note}"

    # 防御资产豁免单只集中度上限：防御仓本来就该满仓短债
    capped = clamp_targets(raw, load_settings().agent.max_position, exempt=defensive_codes(), exposure=exposure)
    return capped, reason, exposure


# ---------------------------------------------------------------- 预测

def _momentum(nav: pd.DataFrame, window: int) -> pd.Series:
    if len(nav) <= window:
        return pd.Series(0.0, index=nav.columns)
    return (nav.iloc[-1] / nav.iloc[-1 - window] - 1.0).fillna(0.0)


def _portfolio_vol(nav: pd.DataFrame, weights: Mapping[str, float], window: int = 20) -> float:
    """持仓加权后的日收益波动率（近似：权重视为期间不变）。"""
    usable = {code: weight for code, weight in weights.items() if code in nav.columns and weight > 0}
    if not usable or len(nav) < window + 1:
        return 0.0
    frame = nav[list(usable)].tail(window + 1)
    returns = frame.pct_change().dropna()
    if returns.empty:
        return 0.0
    total = sum(usable.values())
    weighted = sum(returns[code] * (weight / total) for code, weight in usable.items())
    return float(weighted.std(ddof=0))


def predict(
    pod: Mapping,
    market: MarketView,
    history: pd.DataFrame,
    holdings: Mapping[str, float] | None = None,
) -> dict:
    """次日与 5 日的方向性预测。

    立场：**宁可弃权，也不猜**。
    预测的依据是"当前持仓的 5 日动量相对防御资产的优势"，除以组合波动率得到边际优势；
    边际优势太小时（|dev| < 2%）标记为低置信度，在结算时不记为一次方向性判断。
    这样做的好处是命中率不会再被"永远猜 50.5% 上涨"的废话稀释。
    """
    nav = market.nav
    mom5 = _momentum(nav, 5)
    weights = {code: float(weight) for code, weight in (holdings or {}).items() if float(weight) > 0}
    total = sum(weights.values())

    if weights and total > 0:
        portfolio_mom = sum(weight * float(mom5.get(code, 0.0)) for code, weight in weights.items()) / total
    else:
        portfolio_mom = 0.0
    safe_mom = max((float(mom5.get(code, 0.0)) for code in defensive_codes()), default=0.0)
    edge = portfolio_mom - safe_mom

    vol = _portfolio_vol(nav, weights) or 0.0
    dev = edge / max(vol * 4.0, 0.004)
    if market.has_boards:
        dev += 0.02 * float(market.board_scores["score"].head(3).mean())
    dev = max(min(dev, 0.15), -0.15)

    prob_up = round(0.5 + dev, 4)
    if abs(dev) >= 0.06:
        confidence = "high"
    elif abs(dev) >= 0.02:
        confidence = "mid"
    else:
        confidence = "low"

    ranking = []
    if market.has_boards:
        ranking = [
            {"board": board, "score": round(float(row["score"]), 4)}
            for board, row in market.board_scores.head(5).iterrows()
        ]

    return {
        "prob_up": prob_up,
        "expected_return": round(dev / 10.0, 6),
        "confidence": confidence,
        "board_ranking": ranking,
    }


def forecast_5d_ranking(market: MarketView, top_n: int = 5) -> list[dict]:
    return [
        {"board": board, "score": round(float(row["score"]), 4)}
        for board, row in market.board_scores.head(top_n).iterrows()
    ]


# ---------------------------------------------------------------- 决策卡

def _json_value(value):
    """jsonb 不接受 date/Decimal，统一转成能序列化的形式。"""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (date, pd.Timestamp)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (int, str, bool)):
        return value
    return str(value)


def prices_at(trade_date: date) -> tuple[dict[str, float], date]:
    """取某一交易日（或之前最近一天）的净值，供"晚间按真实净值成交"使用。"""
    codes = list(pod_store.instrument_map().keys())
    nav = load_nav(codes).dropna(how="all").sort_index()
    nav = nav[nav.index <= pd.Timestamp(trade_date)]
    if nav.empty:
        raise RuntimeError(f"{trade_date} 之前没有任何净值数据")
    prices = {
        code: float(value) for code, value in nav.iloc[-1].items() if not pd.isna(value)
    }
    return prices, nav.index[-1].date()


def build_card(
    pod: Mapping,
    market: MarketView,
    targets: Mapping[str, float],
    reason: str,
    fills: Sequence,
    snapshot: Mapping,
    prediction: Mapping,
    exposure: float = 1.0,
    pending: Sequence[Mapping] | None = None,
) -> dict:
    names = {code: row["name"] for code, row in pod_store.instrument_map().items()}
    holdings = (
        "；".join(
            f"{names.get(code, code)} {weight:.0%}"
            for code, weight in targets.items()
            if float(weight) >= 0.005
        )
        or "空仓"
    )
    invested = sum(float(weight) for weight in targets.values())
    if invested < 0.99:
        if exposure < 0.99:
            holdings += f"（风险偏好下调，主动保留 {1 - invested:.0%} 现金）"
        else:
            holdings += f"（受单只上限约束，保留 {1 - invested:.0%} 现金）"
    if fills:
        action = "rebalance"
        traded = "；".join(
            f"{'申购' if fill.side == 'buy' else '赎回'} {names.get(fill.code, fill.code)} ¥{fill.amount:,.0f}"
            for fill in fills
        )
        summary = f"调仓 {len(fills)} 笔：{traded}"
    elif pending:
        # 14:30 的指令：还没成交，只能写"约"，不能写成已发生
        action = "pending"
        traded = "；".join(
            f"{'申购' if item['side'] == 'buy' else '赎回'} "
            f"{names.get(item['code'], item['code'])} 约 ¥{item['amount']:,.0f}"
            for item in pending
        )
        summary = f"待执行 {len(pending)} 笔（14:30 指令）：{traded}"
    else:
        action = "hold"
        summary = "维持不动（与目标的差额低于最小下单额）"

    drawdown = float(snapshot.get("drawdown") or 0.0)
    excess = snapshot.get("excess_return")
    rules = pod["invalidation_rules"] or []
    rule_text = "；".join(rules) if isinstance(rules, list) and rules else "（建仓时未设定）"

    newcomer = (
        "值得跟：建仓条件仍成立且未触发失效条款"
        if drawdown > -0.05 and (excess is None or excess > -0.02)
        else "不建议新进：当前已偏离建仓逻辑或超额为负，等下一个信号"
    )

    prob = prediction["prob_up"]
    benchmark_return = snapshot.get("benchmark_return")
    benchmark_text = (
        f"同期基准 {benchmark_return:+.2%}" if benchmark_return is not None else "同期基准数据缺失"
    )
    excess_text = f"超额 {excess:+.2%}" if excess is not None else "超额待基准数据补齐后计算"

    # 新闻证据：只引用真实抓到的标题，让"看了新闻"这句话可被核查
    news_notes = []
    for item in prediction["board_ranking"][:2]:
        titles = market.news_samples.get(item["board"]) or []
        if not titles:
            continue
        count = int(market.news_counts.get(item["board"], 0)) if market.news_counts is not None else 0
        news_notes.append(f"{item['board']}近 3 日 {count} 条，如《{titles[0]}》")
    news_text = "；相关新闻：" + "；".join(news_notes) if news_notes else "；近 3 日未检索到相关板块新闻"

    if market.is_intraday:
        pricing_text = (
            f"【决策时点 14:30】净值只到 {market.price_as_of}；"
            f"今日涨跌按盘中行情估算（{market.intraday_ts or '—'} 快照），"
            f"请在 15:00 前下单，成交价以今日收盘净值确认。"
        )
    else:
        pricing_text = ""

    card = {
        "action": action,
        "confidence": prediction["prob_up"],
        "summary": summary,
        "rationale": (
            f"{pricing_text}{reason}。本仓目标权重：{holdings}。"
            f"当前累计 {snapshot['cum_return']:+.2%}，"
            f"{benchmark_text}，"
            f"{excess_text}，回撤 {drawdown:.2%}。"
            f"{news_text}。"
        ),
        "tomorrow_outlook": (
            f"次日上涨概率 {prob:.0%}（置信度 {prediction['confidence']}）；"
            f"5 日相对强弱："
            + "，".join(
                f"{item['board']} {item['score']:+.2f}" for item in prediction["board_ranking"][:3]
            )
            if prediction["board_ranking"]
            else f"次日上涨概率 {prob:.0%}（置信度 {prediction['confidence']}）；板块数据缺失，未做排序"
        ),
        "long_term_view": pod["thesis"],
        "invalidation_note": f"失效条件：{rule_text}。当前回撤 {drawdown:.2%}。",
        "newcomer_verdict": newcomer,
        "payload": {
            "targets": {code: round(float(weight), 4) for code, weight in targets.items()},
            "fills": [
                {
                    "code": fill.code,
                    "side": fill.side,
                    "amount": round(fill.amount, 2),
                    "nav": fill.nav,
                    "fee": round(fill.fee, 2),
                }
                for fill in fills
            ],
            "snapshot": {key: _json_value(value) for key, value in snapshot.items()},
            "pending": [dict(item) for item in (pending or [])],
            "market_notes": market.notes,
            "market_flow_yi": market.market_flow,
            "pricing": "intraday" if market.is_intraday else "close",
            "price_as_of": str(market.price_as_of) if market.price_as_of else None,
            "intraday_ts": market.intraday_ts,
            "news": {
                item["board"]: market.news_samples.get(item["board"], [])
                for item in prediction["board_ranking"][:5]
            },
        },
    }
    return card


def save_card(pod_id: int, trade_date: date, card: Mapping, executed: bool = True) -> None:
    """executed=False 表示这只是 14:30 的"指令"，要到晚间净值出来才真正成交。"""
    db.upsert(
        "pod_decision",
        [
            "pod_id", "trade_date", "action", "confidence", "summary", "rationale",
            "tomorrow_outlook", "long_term_view", "invalidation_note", "newcomer_verdict",
            "payload", "model", "prompt_version", "executed_at",
        ],
        [
            (
                pod_id, trade_date, card["action"], card["confidence"], card["summary"],
                card["rationale"], card["tomorrow_outlook"], card["long_term_view"],
                card["invalidation_note"], card["newcomer_verdict"],
                Jsonb(card["payload"]), card.get("model", "rule-template"), card.get("prompt_version", "v1"),
                pd.Timestamp.now(tz="Asia/Shanghai") if executed else None,
            )
        ],
        conflict=["pod_id", "trade_date"],
    )


def save_prediction(pod_id: int, trade_date: date, prediction: Mapping, horizon: int = 1) -> None:
    db.upsert(
        "pod_prediction",
        ["pod_id", "trade_date", "horizon_days", "target", "prob_up", "expected_return", "board_ranking", "confidence"],
        [
            (
                pod_id, trade_date, horizon, "pod_nav", prediction["prob_up"],
                prediction["expected_return"], Jsonb(prediction["board_ranking"]), prediction["confidence"],
            )
        ],
        conflict=["pod_id", "trade_date", "horizon_days", "target"],
    )
