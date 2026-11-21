"""Point-in-time, falsifiable explanations of sector order flow and price response."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import isfinite
from statistics import mean, median
from zoneinfo import ZoneInfo

from . import db

VERSION = 1
SH = ZoneInfo("Asia/Shanghai")
SOURCE_NAMES = {"ths": "同花顺板块指数", "eastmoney": "东方财富板块指数"}
LABELS = {
    "demand": "买方承接占优",
    "distribution": "上涨后放量回落，兑现压力待验证",
    "supply": "卖压占优",
    "rotation": "资金方向切换，调仓换手待验证",
    "mixed": "价量与资金尚无一致方向",
    "insufficient": "资料不足",
}


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _stamp(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError("行为研究时间必须包含时区")
    return dt.astimezone(SH)


def _number(value):
    if value is None:
        return None
    n = float(value)
    return n if isfinite(n) else None


def load_bars(board_ids, closed_sessions, as_of):
    """One batch for the candidate set; preserve missing sessions when computing returns."""
    cutoff = _stamp(as_of)
    days = sorted(
        {
            _day(d)
            for d in closed_sessions
            if _day(d) < cutoff.date() or (_day(d) == cutoff.date() and cutoff.time() >= time(15))
        }
    )[-21:]
    ids = list(set(board_ids))
    if not ids or not days:
        return {}
    rows = db.query(
        f"""SELECT q.board_id,q.trade_date,q.source,q.ingested_at,q.open::float,q.high::float,
               q.low::float,q.close::float,q.amount::float,
               ix.pct_chg::float benchmark_pct,ix.source benchmark_source
            FROM {db.qualified("board_quote")} q JOIN {db.qualified("board")} b USING(board_id)
            LEFT JOIN {db.qualified("index_quote")} ix ON ix.index_code='000300'
              AND ix.trade_date=q.trade_date AND ix.ingested_at<=%s
            WHERE q.board_id=ANY(%s) AND q.trade_date=ANY(%s) AND q.ingested_at<=%s
              AND q.source=b.source ORDER BY q.board_id,q.trade_date""",
        [cutoff, ids, days, cutoff],
    )
    grouped = defaultdict(dict)
    for row in rows:
        grouped[row["board_id"]][row["trade_date"]] = row
    return {
        bid: [bars.get(day, {"trade_date": day}) for day in days] for bid, bars in grouped.items()
    }


def _price_features(bars, market_day, cutoff):
    visible = []
    for bar in bars:
        day = _day(bar["trade_date"])
        if day > market_day or datetime.combine(day, time(15), SH) > cutoff:
            continue
        if bar.get("ingested_at") is None or _stamp(bar["ingested_at"]) > cutoff:
            visible.append({"trade_date": day})
        else:
            visible.append(bar)
    visible.sort(key=lambda b: _day(b["trade_date"]))
    if not visible or _day(visible[-1]["trade_date"]) != market_day:
        return {}
    bar = visible[-1]
    source = bar.get("source")
    if not source:
        return {}

    def close_at(offset):
        if len(visible) < offset or visible[-offset].get("source") != source:
            return None
        n = _number(visible[-offset].get("close"))
        return n if n and n > 0 else None

    close, previous, before_run = (close_at(offset) for offset in (1, 2, 7))
    if close is None:
        return {}
    opening, low, high, amount = (_number(bar.get(k)) for k in ("open", "low", "high", "amount"))
    valid_range = (
        low is not None and high is not None and low > 0 and low <= close <= high and high > low
    )
    valid_open = valid_range and opening is not None and low <= opening <= high
    earlier = [_number(b.get("amount")) for b in visible[-21:-1] if b.get("source") == source]
    earlier = [n for n in earlier if n is not None and n > 0]
    return {
        "source": SOURCE_NAMES.get(source, source),
        "provider": source,
        "close": close,
        "high": high if valid_range else None,
        "low": low if valid_range else None,
        "benchmark_pct": _number(bar.get("benchmark_pct")),
        "benchmark_source": bar.get("benchmark_source"),
        "day_return": close / previous - 1 if previous else None,
        "prior_5d_return": previous / before_run - 1 if previous and before_run else None,
        "close_location": (close - low) / (high - low) if valid_range else None,
        "upper_wick": (high - max(close, opening)) / (high - low) if valid_open else None,
        "high_vs_previous": high / previous - 1 if valid_range and previous else None,
        "amount_ratio": amount / median(earlier)
        if amount and amount > 0 and len(earlier) >= 10
        else None,
        "baseline_days": len(earlier),
    }


HYPOTHESES = {
    "demand": (
        "买方可能在持续增加风险敞口",
        "增配者可能愿意承接获利盘；被动申购或指数调权也能造成相似的单日表现。",
        "下一收盘高于基准收盘，且核验净流入延续。",
        "收盘转跌并出现核验净流出，持续承接解释减弱。",
    ),
    "distribution": (
        "前期持有人可能在高位兑现",
        "此前上涨提供了获利空间；跟随买盘可能被已有持仓的卖单吸收。集中赎回也可能产生同样的价格反应。",
        "接下来收盘无法收复信号日高点，放量走弱和净流出再次同时出现。",
        "收盘收复信号日高点且再获净流入，兑现压力解释减弱。",
    ),
    "supply": (
        "减仓或赎回带来的卖压",
        "降低风险或应对赎回的卖方可能更急于成交，买方可能等待更低价格。单凭下跌无法区分赎回、止损和获利兑现。",
        "下一收盘继续走低且净额仍为负。",
        "价格止跌、净额转正并能维持到收盘，卖压延续解释减弱。",
    ),
    "rotation": (
        "短期调仓或被动换手",
        "组合调权、被动申赎与不同持有期限的交易者换手，可能暂时打破此前的资金方向。",
        "后续净额回到此前五日方向，单日价格冲击消退。",
        "新方向跨多个收盘持续，并伴随同向价格变化，短期换手解释减弱。",
    ),
    "mixed": (
        "买卖双方尚未形成单边优势",
        "获利了结者与新进入者可能各自成交，市场仍在寻找愿意承接的价格。",
        "未来收盘和资金净额仍反复，未出现同向延续。",
        "出现连续同向的价格与资金变化后，重新检验承接或卖压解释。",
    ),
    "market": (
        "跟随大盘降低风险",
        "赎回宽基或普遍降低股票仓位，会使板块与大盘一起承压；这也能解释板块的资金流出。",
        "板块与沪深300继续同向，止跌时间接近。",
        "沪深300企稳而板块仍放量走弱，应重新评估板块特有卖压。",
    ),
}

# Conditional checks on each subsequent session; do not fit a path to future prices.
CHECKPOINTS = (
    "首个后续收盘的方向",
    "第二日有无持续成交",
    "第三日量能和价格反应",
    "第四日资金与价格能否相互验证",
    "第五日累计表现及预期失效条件",
)
SCENARIO_PATHS = {
    "demand": (
        ("收盘高于预测基准，买方仍愿意承接。", "收盘下跌并伴随同源净流出。"),
        ("连续两日净额为正，首日涨幅大致保留。", "首日涨幅完全回吐，新增买盘未能持续。"),
        ("放量时收盘接近当日高位。", "成交放大但多次冲高回落。"),
        ("回撤日成交额收缩，随后收盘能回到信号日收位之上。", "回撤日放量且资金转负。"),
        ("已冻结标的五日收益为正，板块五日资金榜同为正。", "五日收益为负，承接未转化为持有收益。"),
    ),
    "distribution": (
        ("收盘转弱且未收复信号日高点。", "收盘突破该高点，且净额转正。"),
        ("反弹时仍有净流出，买方未能吸收抛盘。", "连续两日收强，资金同步转正。"),
        ("下跌日成交放大，反弹日成交收缩。", "放量上涨且收盘接近最高位。"),
        ("在前高附近再次回落，收盘持续低于信号日收位。", "收盘站上前高并连续保持。"),
        ("五日收益为负，期间未能收复信号日高点。", "五日收益为正且已突破该高点。"),
    ),
    "supply": (
        ("板块续跌，净额依旧为负。", "价格与资金方向同时转正。"),
        ("两次收盘均偏弱，反弹承接有限。", "次日出现强收盘且前一日跌幅收复。"),
        ("同源资金继续流出，跌势伴随放量。", "放量下跌结束，资金和收盘同步转强。"),
        (
            "大盘企稳时板块仍走弱，卖压可能来自板块内部。",
            "板块与大盘同步企稳，普遍降风险更能解释此前变动。",
        ),
        ("标的五日收益为负，板块五日榜净额为负。", "五日收益为正，卖压没有持续压低标的价格。"),
    ),
    "rotation": (
        ("单日资金方向反复，未形成同向突破。", "资金和价格均沿新方向突破。"),
        ("反向冲击消退，收盘回到信号日附近。", "新方向延续两次收盘。"),
        ("成交额回归近期基线，价格波动收敛。", "放量并连续收于区间同一侧。"),
        ("资金再度转向，价格未远离信号日收位。", "单边净额持续且价格跟随。"),
        (
            "届时的五日榜未确认单日方向，日间冲击消退。",
            "五日累计价格与新资金方向一致，需重新评估趋势。",
        ),
    ),
    "mixed": (
        ("归档下一收盘方向和核验净额，观察分歧是否持续。", "价格与资金同时转向一边。"),
        ("两个收盘的价格或资金方向反复。", "相同方向延续两个收盘。"),
        ("放量未带来一致的收盘方向。", "量、价和同源资金同时支持一个方向。"),
        ("价格在信号日区间内反复。", "连续收盘脱离区间且资金方向一致。"),
        (
            "对照五日净额和标的实际收益，检查单边延续是否依然缺乏证据。",
            "五日累计价格与资金显示持续的同向变动。",
        ),
    ),
}


def analyze(row, bars, as_of):
    cutoff, market_day = _stamp(as_of), _day(row["as_of"])
    prices = _price_features(bars, market_day, cutoff)
    observations = []

    def record(key, label, value, unit, at, source):
        if value is not None:
            observations.append(
                {
                    "key": key,
                    "label": label,
                    "value": round(value, 4),
                    "unit": unit,
                    "at": str(at),
                    "source": source,
                }
            )

    for key, label in (
        ("day_return", "板块当日涨跌"),
        ("prior_5d_return", "信号日前五日涨跌"),
        ("close_location", "收盘位于当日高低区间的位置"),
        ("upper_wick", "上影线占当日振幅"),
        ("high_vs_previous", "当日最高价相对昨收"),
    ):
        if prices.get(key) is not None:
            record(key, label, prices[key] * 100, "%", market_day, prices["source"])
    for key, label in (
        ("close", "信号日板块收盘"),
        ("high", "信号日板块高点"),
        ("low", "信号日板块低点"),
    ):
        record(key, label, prices.get(key), "点", market_day, prices.get("source"))
    benchmark_pct = prices.get("benchmark_pct")
    if benchmark_pct is not None:
        record(
            "benchmark_pct",
            "同期沪深300涨跌",
            benchmark_pct,
            "%",
            market_day,
            f"沪深300 · {prices.get('benchmark_source') or '指数收盘行情'}",
        )
    record(
        "amount_ratio",
        f"成交额相对前{prices.get('baseline_days', 0)}个有效交易日中位数",
        prices.get("amount_ratio"),
        "倍",
        market_day,
        prices.get("source"),
    )
    flow = row.get("capital_flow") or {}
    verified = (
        flow.get("flow_status") == "available"
        and flow.get("flow_date") == str(market_day)
        and flow.get("flow_source") in ("同花顺", "东方财富")
        and datetime.combine(market_day, time(15), SH) <= cutoff
    )
    net = _number(flow.get("net_yuan")) if verified else None
    five = _number(flow.get("net_5d_yuan")) if verified else None
    flow_source = f"{flow.get('flow_source')} · {flow.get('flow_method') or '板块资金榜'}"
    for key, label, n in (("net_1d", "收盘净额", net), ("net_5d", "源站五日榜净额", five)):
        if n is not None:
            record(key, label, n / 1e8, "亿元", market_day, flow_source)
    if verified:
        record(
            "flow_pct", "收盘资金净占比", _number(flow.get("net_pct")), "%", market_day, flow_source
        )

    spot = row.get("intraday") or {}
    spot_time = _stamp(spot["at"]) if spot.get("at") else None
    fresh_spot = (
        spot_time is not None
        and spot_time.date() == cutoff.date()
        and spot_time.weekday() < 5
        and (
            time(9, 30) <= spot_time.time() <= time(11, 30)
            or time(13) <= spot_time.time() <= time(15)
        )
        and timedelta(0) <= cutoff - spot_time <= timedelta(minutes=30)
    )
    spot_net = None
    change = None
    if fresh_spot:
        change = _number(spot.get("return"))
        if change is not None:
            record(
                "intraday_return",
                "盘中累计涨跌",
                change * 100,
                "%",
                spot_time.isoformat(),
                f"{spot.get('source') or '板块盘中快照'}"
                + (f" · {spot['source_code']}" if spot.get("source_code") else ""),
            )
        spot_flow = spot.get("capital_flow") or {}
        if spot_flow.get("source") == "东方财富板块行情":
            spot_net = _number(spot_flow.get("net_yuan"))
            if spot_net is not None:
                record(
                    "intraday_net",
                    "盘中主力净流入估算",
                    spot_net / 1e8,
                    "亿元",
                    spot_time.isoformat(),
                    spot_flow["source"],
                )

    direction, location = prices.get("day_return"), prices.get("close_location")
    support, against = defaultdict(list), defaultdict(list)
    broad_selling = (
        direction is not None and direction < 0 and benchmark_pct is not None and benchmark_pct < 0
    )
    if broad_selling:
        support["market"].append(
            f"板块下跌{abs(direction):.2%}，同期沪深300下跌{abs(benchmark_pct):.2f}%。"
        )
    if direction is not None and direction != 0:
        name = "demand" if direction > 0 else "supply"
        support[name].append(f"板块收盘相对昨收{direction:+.2%}。")
    if net is not None and net != 0:
        name = "demand" if net > 0 else "supply"
        support[name].append(f"{flow['flow_source']}当日净额{net / 1e8:+.2f}亿元。")
    opposite_windows = net is not None and five is not None and net * five < 0
    if opposite_windows:
        support["rotation"].append("同源单日净额与五日榜净额方向相反。")
    run = prices.get("prior_5d_return")
    ran_up = run is not None and run > 0
    faded = (
        location is not None
        and location <= 1 / 3
        and (prices.get("high_vs_previous") or 0) > 0
        and (prices.get("upper_wick") or 0) >= 1 / 3
    )
    heavy = (prices.get("amount_ratio") or 0) >= 1.2
    if ran_up:
        support["distribution"].append(f"信号日前五日累计上涨{run:.2%}，已有获利空间。")
    if faded:
        support["distribution"].append(
            "日内高点高于昨收，收在振幅下三分之一，上影线占振幅至少三分之一。"
        )
    if heavy:
        support["distribution"].append(
            f"成交额为此前有效样本中位数的{prices['amount_ratio']:.2f}倍。"
        )
    if net is not None and net < 0:
        support["distribution"].append(support["supply"][-1])

    if direction is None or location is None or net is None:
        regime = "insufficient"
    elif ran_up and faded and heavy and net < 0:
        regime = "distribution"
    elif opposite_windows or direction * net < 0:
        regime = "rotation"
        if direction * net < 0:
            support[regime].append("当日收盘涨跌与资金净额方向相反。")
    elif direction > 0 and net > 0 and location >= 2 / 3:
        regime = "demand"
    elif direction < 0 and net < 0 and location <= 1 / 3:
        regime = "supply"
    else:
        regime = "mixed"
        support[regime].append("当前收盘位置、价格方向和资金净额未同时满足单边信号。")

    against["demand"] = support["supply"].copy()
    against["supply"] = support["demand"].copy()
    against["distribution"] = support["demand"].copy()
    previous = None
    same_board = spot.get("source") == {
        "ths": "同花顺板块分时",
        "eastmoney": "东方财富板块行情",
    }.get(prices.get("provider"))
    if (
        fresh_spot
        and change is not None
        and same_board
        and spot.get("source_code")
        and market_day < spot_time.date()
        and prices.get("low") is not None
    ):
        spot_price = prices["close"] * (1 + change)
        conflict = None
        if regime == "demand" and spot_price < prices["low"]:
            conflict = "同源分时已跌破信号日低点，买方承接有待收盘确认。"
        elif regime in ("supply", "distribution") and spot_price > prices["high"]:
            conflict = "同源分时已越过信号日高点，卖方压力有待收盘确认。"
        if conflict:
            against[regime].append(conflict)
            if regime in ("demand", "supply"):
                previous = regime
                regime = "mixed"
                support[regime].append(conflict)
    if spot_net is not None and net is not None and spot_net * net < 0:
        conflict = "当次盘中主力估算与最近收盘净额方向相反；时间和口径有别，需等待同源收盘复核。"
        if regime in ("demand", "supply", "distribution"):
            against[regime].append(conflict)
            support["mixed"].append(conflict)
            previous = regime
            regime = "mixed"
        else:
            support["mixed"].append(conflict)
    primary = "mixed" if regime == "insufficient" else regime
    if regime in ("supply", "distribution") and broad_selling:
        alternative = "market"
    elif regime in ("supply", "distribution", "demand", "insufficient"):
        alternative = "rotation"
    elif previous:
        alternative = previous
    else:
        alternative = "supply" if net is not None and net < 0 else "demand"
    hypotheses = [
        {
            "key": k,
            "title": HYPOTHESES[k][0],
            "interpretation": HYPOTHESES[k][1],
            "support": support[k],
            "against": against[k],
            "confirm": HYPOTHESES[k][2],
            "invalidate": HYPOTHESES[k][3],
        }
        for k in (primary, alternative)
    ]
    limits = ["公开资金榜与价格不能识别交易者身份、成本或真实动机；同一现象可有多种解释。"]
    if net is None:
        limits.append("该交易日缺少已核验的板块净额，资金方向待核。")
    if fresh_spot and spot_net is None:
        limits.append("本次同源分时只有价格，缺少带交易时间的该板块盘中资金净额。")
    if prices.get("amount_ratio") is None:
        limits.append("成交额历史基线不足，无法判断是否放量。")
    if row.get("proxy_code"):
        limits.append(f"行为证据属于板块；正式预测按代理基金{row['proxy_code']}的净值单独验证。")
    if regime == "insufficient":
        limits.append("价格区间、前一收盘或资金净额缺失，暂不据此改变风险预算。")
    risk = 0.75 if regime == "distribution" else 1.0
    return {
        "version": VERSION,
        "regime": regime,
        "label": LABELS[regime],
        "as_of": cutoff.isoformat(),
        "market_day": str(market_day),
        "observations": observations,
        "hypotheses": hypotheses,
        "path": [
            {
                "day": i,
                "watch": watch,
                "confirm": confirm,
                "invalidate": invalidate,
            }
            for i, (watch, (confirm, invalidate)) in enumerate(
                zip(CHECKPOINTS, SCENARIO_PATHS[primary], strict=True), 1
            )
        ],
        "limitations": limits,
        "risk_multiplier": risk,
        "risk_reason": (
            "出现此前上涨、冲高弱收、放量与净流出组合，新增风险预算乘0.75。"
            if risk < 1
            else "未触发放量冲高回落规则，新增风险预算维持原值。"
        ),
    }


def feedback(samples, *, board_id, proxy_code, horizon_days, regime, as_of):
    """Estimate the mature, non-overlapping error against the probability frozen at issue."""
    cutoff = _stamp(as_of)
    eligible = []
    for s in samples:
        if (
            s.get("board_id") != board_id
            or s.get("proxy_code") != proxy_code
            or s.get("horizon_days") != horizon_days
            or s.get("regime") != regime
            or s.get("behavior_version", s.get("version", VERSION)) != VERSION
            or not s.get("scored_at")
            or s.get("realized_return") is None
            or s.get("base_prob_up") is None
        ):
            continue
        start, end, score = _day(s["base_date"]), _day(s["target_date"]), _stamp(s["scored_at"])
        due = datetime.combine(end, time(15), SH)
        if not (
            start < end and end >= cutoff.date() - timedelta(days=365) and due <= score <= cutoff
        ):
            continue
        if s.get("issued_at") and _stamp(s["issued_at"]) >= due:
            continue
        actual, base = _number(s["realized_return"]), _number(s["base_prob_up"])
        if actual is not None and base is not None and 0 <= base <= 1:
            eligible.append((end, start, actual, base, _number(s.get("prob_up"))))
    outcomes, estimates, dates = [], [], []
    paired_base, paired_issued = [], []
    next_start = cutoff.date()
    for end, start, actual, base, published in sorted(
        eligible, key=lambda s: (s[0], s[1]), reverse=True
    ):
        if end > next_start:
            continue
        outcomes.append(actual)
        estimates.append(base)
        dates.append(end)
        if published is not None and 0 <= published <= 1:
            outcome = int(actual > 0)
            paired_base.append((base - outcome) ** 2)
            paired_issued.append((published - outcome) ** 2)
        next_start = start
        if len(outcomes) == 60:
            break
    n = len(outcomes)
    errors = [int(r > 0) - p for r, p in zip(outcomes, estimates, strict=True)]
    base_score = mean(paired_base) if paired_base else None
    issued_score = mean(paired_issued) if paired_issued else None
    shift = (
        max(-0.05, min(0.05, sum(errors) / (n + 30)))
        if n >= 12 and regime != "insufficient"
        else 0.0
    )
    return {
        "samples": n,
        "first_target": str(dates[-1]) if n else None,
        "last_target": str(dates[0]) if n else None,
        "up_rate": mean(r > 0 for r in outcomes) if n else None,
        "mean_return": mean(outcomes) if n else None,
        "mean_base_probability": mean(estimates) if n else None,
        "brier_base": mean(e**2 for e in errors) if n else None,
        "paired_samples": len(paired_base),
        "brier_base_paired": base_score,
        "brier_adjusted": issued_score,
        "brier_gain": base_score - issued_score if paired_base else None,
        "probability_shift": shift,
        "min_samples": 12,
    }
