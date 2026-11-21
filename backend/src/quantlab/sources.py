"""数据抓取层：唯一与 akshare 打交道的地方。

为什么这里写得这么啰嗦——限流是这个项目最重要的外部约束：

* 东方财富 push2 接口按 IP 限流，一次触发轻则约 5 分钟、重则 40 分钟不可用，
  而且**反复重试会延长封禁时间**；
* 所以所有调用串行走 ``Throttle``（间隔 + 抖动），
* 一旦识别到限流就抛 ``RateLimited`` 让上层立刻中止本轮，绝不在循环里重试轰炸。

对外只暴露"返回规范化 DataFrame"的函数，列名映射集中在本文件，
akshare 改字段名时只需要改这里。
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from . import usage

log = logging.getLogger(__name__)

# 命中这些关键字就认为是限流/断连，立即中止本轮
_RATE_LIMIT_HINTS = ("限流", "RateLimit", "RemoteDisconnected", "Connection aborted", "Max retries")


class RateLimited(RuntimeError):
    """数据源限流。上层应当中止本轮并安排下次重试，而不是继续请求。"""


@dataclass
class Throttle:
    """串行限速器：保证两次外部请求之间至少间隔 min_interval 秒。"""

    min_interval: float = 1.2
    jitter: float = 1.0
    _last: float = field(default=0.0, repr=False)

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        delay = self.min_interval + random.uniform(0, self.jitter) - elapsed
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def slow_down(self, factor: float = 2.0, cap: float = 8.0) -> None:
        self.min_interval = min(self.min_interval * factor, cap)
        log.warning("抓取间隔放慢到 %.1fs", self.min_interval)


THROTTLE = Throttle(
    min_interval=float(os.getenv("QUANTLAB_THROTTLE_MIN", "1.2")),
    jitter=float(os.getenv("QUANTLAB_THROTTLE_JITTER", "1.0")),
)


def _call(func, provider, operation, **kwargs):
    """带限速的单次外部调用。限流直接抛出，不做重试。"""
    THROTTLE.wait()
    try:
        with usage.attempt(
            kind="data", provider=provider, operation=operation,
            detail="按 AKShare 适配器调用计数，内部缓存、分页和重试未逐次计量。",
        ):
            return func(**kwargs)
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        if any(hint in text for hint in _RATE_LIMIT_HINTS):
            raise RateLimited(f"{getattr(func, '__name__', func)} 被限流或断连: {exc}") from exc
        raise


def _pick(df: pd.DataFrame, *candidates: str) -> str | None:
    for name in candidates:
        if name in df.columns:
            return name
    return None


def _rename(df: pd.DataFrame, mapping: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    """把 akshare 的中文列名映射成我们的规范列名；缺失的列补 None。"""
    out = pd.DataFrame(index=df.index)
    missing: list[str] = []
    for target, candidates in mapping.items():
        source = _pick(df, *candidates)
        if source is None:
            missing.append(target)
            out[target] = None
        else:
            out[target] = df[source]
    if missing:
        log.warning("字段缺失 %s，原始列=%s", missing, list(df.columns))
    return out


def _to_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.date


def _to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


# ================================================================ 日历与基金

def fetch_trade_calendar() -> pd.DataFrame:
    import akshare as ak

    df = _call(ak.tool_trade_date_hist_sina, "新浪财经", "读取交易日历")
    frame = _rename(df, {"trade_date": ("trade_date", "日期")})
    frame["trade_date"] = _to_date(frame["trade_date"])
    return frame.dropna().drop_duplicates().sort_values("trade_date").reset_index(drop=True)


def fetch_fund_nav(code: str, since: date | None = None, preferred: str = "eastmoney-lsjz") -> pd.DataFrame:
    """Read published NAVs. Both feeds supply actual units rather than intraday estimates."""
    import json
    import re

    import requests

    def get(url, operation, **kwargs):
        THROTTLE.wait()
        with usage.attempt(kind="data", provider="东方财富", operation=operation, detail="基金净值 HTTP GET 请求。"):
            response = requests.get(url, timeout=(4, 12), headers={"Referer": "https://fundf10.eastmoney.com/"}, **kwargs)
            if response.status_code == 429:
                raise RateLimited("东方财富净值源限流")
            response.raise_for_status()
            return response

    def history():
        rows = []
        page = 1
        while True:
            params = {"fundCode": code, "pageIndex": page, "pageSize": 100}
            if since is not None:
                params["sdate"] = since.isoformat()
            result = get("https://api.fund.eastmoney.com/f10/lsjz", "读取基金净值明细", params=params).json()
            if str(result.get("ErrCode", "0")) != "0":
                raise ValueError("净值明细接口返回错误")
            batch = result["Data"]["LSJZList"]
            if not isinstance(batch, list):
                raise ValueError("净值明细字段不完整")
            rows.extend((r["FSRQ"], r["DWJZ"], r.get("LJJZ")) for r in batch)
            if not batch or len(rows) >= int(result["TotalCount"]):
                break
            page += 1
        return pd.DataFrame(rows, columns=["trade_date", "nav", "acc_nav"])

    def published_chart():
        script = get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", "读取基金净值走势").text

        def series(name):
            match = re.search(r"\bvar\s+" + name + r"\s*=\s*(\[[^;]*?\])\s*;", script)
            return json.loads(match[1]) if match else []

        units = series("Data_netWorthTrend")
        cumulative = dict(series("Data_ACWorthTrend") or [])
        frame = pd.DataFrame([(r["x"], r["y"], cumulative.get(r["x"])) for r in units],
                             columns=["trade_date", "nav", "acc_nav"])
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        return frame

    feeds = {"eastmoney-lsjz": history, "eastmoney-pingzhong": published_chart}
    order = sorted(feeds, key=lambda source: source != preferred)
    for index, source in enumerate(order):
        try:
            frame = feeds[source]()
            frame["trade_date"] = _to_date(frame["trade_date"])
            frame["nav"] = _to_num(frame["nav"])
            frame["acc_nav"] = _to_num(frame["acc_nav"])
            frame = frame.dropna(subset=["trade_date", "nav"])
            frame = frame[frame["nav"].gt(0) & frame["nav"].lt(float("inf"))]
            if since is not None:
                frame = frame[frame["trade_date"] >= since]
            if frame.empty:
                raise ValueError("当前时段没有有效单位净值")
            frame = frame.drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)
            frame.attrs["source"] = source
            return frame
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            if index == len(order) - 1:
                raise
            log.warning("%s %s 读取失败（%s），切换公开净值接口", code, source, type(exc).__name__)


def fetch_fund_list() -> pd.DataFrame:
    """全市场基金名录，用于扩充基金池（按名称/类型筛 C 类指数基金）。"""
    import akshare as ak

    df = _call(ak.fund_name_em, "东方财富", "读取基金名录")
    frame = _rename(
        df,
        {"code": ("基金代码",), "name": ("基金简称",), "fund_type": ("基金类型",), "pinyin": ("拼音缩写",)},
    )
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    return frame


# ================================================================ 板块与资金流

def fetch_boards(kind: str = "industry") -> pd.DataFrame:
    """板块清单。kind: industry / concept。"""
    import akshare as ak

    func = ak.stock_board_industry_name_em if kind == "industry" else ak.stock_board_concept_name_em
    df = _call(func, "东方财富", "读取板块目录")
    frame = _rename(df, {"code": ("板块代码", "代码"), "name": ("板块名称", "名称")})
    frame = frame.dropna(subset=["code", "name"])
    frame["code"] = frame["code"].astype(str).str.strip()
    frame["name"] = frame["name"].astype(str).str.strip()
    frame["kind"] = kind
    return frame.drop_duplicates(subset=["code"]).reset_index(drop=True)


def fetch_board_hist(name: str, start: str, end: str) -> pd.DataFrame:
    """单个板块的日线行情。start/end 为 YYYYMMDD。"""
    import akshare as ak

    df = _call(
        ak.stock_board_industry_hist_em, "东方财富", "读取行业板块日线",
        symbol=name,
        period="日k",
        start_date=start,
        end_date=end,
        adjust="",
    )
    frame = _rename(
        df,
        {
            "trade_date": ("日期",),
            "open": ("开盘",),
            "high": ("最高",),
            "low": ("最低",),
            "close": ("收盘",),
            "pct_chg": ("涨跌幅",),
            "amplitude": ("振幅",),
            "turnover_rate": ("换手率",),
            "amount": ("成交额",),
            "volume": ("成交量",),
        },
    )
    frame["trade_date"] = _to_date(frame["trade_date"])
    for col in ("open", "high", "low", "close", "pct_chg", "amplitude", "turnover_rate", "amount", "volume"):
        frame[col] = _to_num(frame[col])
    return frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)


def fetch_board_hist_concept(name: str, start: str, end: str) -> pd.DataFrame:
    """概念板块日线（接口与行业板块不同）。"""
    import akshare as ak

    df = _call(
        ak.stock_board_concept_hist_em, "东方财富", "读取概念板块日线",
        symbol=name,
        period="daily",
        start_date=start,
        end_date=end,
        adjust="",
    )
    return fetch_board_hist_frame(df)


def fetch_board_hist_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = _rename(
        df,
        {
            "trade_date": ("日期",),
            "open": ("开盘",),
            "high": ("最高",),
            "low": ("最低",),
            "close": ("收盘",),
            "pct_chg": ("涨跌幅",),
            "amplitude": ("振幅",),
            "turnover_rate": ("换手率",),
            "amount": ("成交额",),
            "volume": ("成交量",),
        },
    )
    frame["trade_date"] = _to_date(frame["trade_date"])
    for col in ("open", "high", "low", "close", "pct_chg", "amplitude", "turnover_rate", "amount", "volume"):
        frame[col] = _to_num(frame[col])
    return frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)


def fetch_board_flow_rank(indicator: str = "今日", sector_type: str = "行业资金流") -> pd.DataFrame:
    """板块资金流排行榜（当日截面）。indicator: 今日 / 5日 / 10日。"""
    import akshare as ak

    df = _call(ak.stock_sector_fund_flow_rank, "东方财富", "读取板块资金流", indicator=indicator, sector_type=sector_type)
    frame = _rename(
        df,
        {
            "name": ("名称",),
            "close_pct": ("今日涨跌幅", f"{indicator}涨跌幅"),
            "main_net": ("今日主力净流入-净额", f"{indicator}主力净流入-净额"),
            "main_net_pct": ("今日主力净流入-净占比", f"{indicator}主力净流入-净占比"),
            "super_large_net": ("今日超大单净流入-净额", f"{indicator}超大单净流入-净额"),
            "large_net": ("今日大单净流入-净额", f"{indicator}大单净流入-净额"),
            "medium_net": ("今日中单净流入-净额", f"{indicator}中单净流入-净额"),
            "small_net": ("今日小单净流入-净额", f"{indicator}小单净流入-净额"),
        },
    )
    frame = frame.dropna(subset=["name"])
    frame["name"] = frame["name"].astype(str).str.strip()
    for col in ("close_pct", "main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net"):
        frame[col] = _to_num(frame[col])
    return frame.reset_index(drop=True)


def fetch_market_flow() -> pd.DataFrame:
    """大盘资金流历史。"""
    import akshare as ak

    df = _call(ak.stock_market_fund_flow, "东方财富", "读取市场资金流")
    frame = _rename(
        df,
        {
            "trade_date": ("日期",),
            "close_pct": ("上证-涨跌幅", "沪深-涨跌幅"),
            "main_net": ("主力净流入-净额",),
            "main_net_pct": ("主力净流入-净占比",),
            "super_large_net": ("超大单净流入-净额",),
            "large_net": ("大单净流入-净额",),
            "medium_net": ("中单净流入-净额",),
            "small_net": ("小单净流入-净额",),
        },
    )
    frame["trade_date"] = _to_date(frame["trade_date"])
    for col in ("close_pct", "main_net", "main_net_pct", "super_large_net", "large_net", "medium_net", "small_net"):
        frame[col] = _to_num(frame[col])
    return frame.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)


# ================================================================ 指数与新闻

def fetch_index_daily(code: str = "000300", start: str = "20150101", end: str = "20991231") -> pd.DataFrame:
    """基准指数日线。东财限流时退回新浪——超额收益是"留哪个仓"的核心依据，不能缺。"""
    import akshare as ak

    try:
        df = _call(
            ak.index_zh_a_hist, "东方财富", "读取基准指数日线", symbol=code, period="daily", start_date=start, end_date=end
        )
        frame = _rename(df, {"trade_date": ("日期",), "close": ("收盘",), "pct_chg": ("涨跌幅",)})
        frame["source"] = "eastmoney"
    except RateLimited as exc:
        log.warning("东方财富指数接口限流，改用新浪：%s", exc)
        frame = _index_from_sina(ak, code)
    except Exception as exc:  # noqa: BLE001
        log.warning("index_zh_a_hist 失败，改用新浪：%s", exc)
        frame = _index_from_sina(ak, code)

    frame["trade_date"] = _to_date(frame["trade_date"])
    frame["close"] = _to_num(frame["close"])
    if "pct_chg" in frame.columns:
        frame["pct_chg"] = _to_num(frame["pct_chg"])
    return frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)


def _index_from_sina(ak, code: str) -> pd.DataFrame:
    """新浪指数日线（sh000300 / sz399006 形式），不带涨跌幅，用收盘价补算。"""
    if code.startswith(("sh", "sz")):
        symbol = code
    elif code.startswith("399"):
        # 深证成指 / 创业板指在深交所，前缀必须是 sz
        symbol = f"sz{code}"
    else:
        symbol = f"sh{code}"
    df = _call(ak.stock_zh_index_daily, "新浪财经", "读取基准指数日线", symbol=symbol)
    frame = _rename(df, {"trade_date": ("date", "日期"), "close": ("close", "收盘")})
    frame["trade_date"] = _to_date(frame["trade_date"])
    frame["close"] = _to_num(frame["close"])
    frame = frame.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    frame["pct_chg"] = frame["close"].pct_change() * 100.0
    frame["source"] = "sina"
    return frame


def _hash_news(source: str, title: str, published_at: datetime) -> str:
    raw = f"{source}|{title.strip()}|{published_at.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fetch_news_cls() -> pd.DataFrame:
    """财联社电报。接口返回标题可能为空，正文里带【标题】前缀。"""
    import akshare as ak

    df = _call(ak.stock_info_global_cls, "财联社", "抓取财经电报", symbol="全部")
    rows = []
    for item in df.to_dict(orient="records"):
        title = str(item.get("标题") or "").strip()
        body = str(item.get("内容") or "").strip()
        if not title and body.startswith("【") and "】" in body:
            title = body[1 : body.index("】")]
        if not title:
            title = body[:40]
        published = _combine(datetime.combine(item["发布日期"], item["发布时间"]))
        if published is None:
            continue
        rows.append(
            {
                "source": "cls",
                "external_id": None,
                "published_at": published,
                "title": title,
                "body": body,
                "url": None,
            }
        )
    return _finalize_news(rows)


def fetch_news_em() -> pd.DataFrame:
    """东方财富财经快讯。"""
    import akshare as ak

    df = _call(ak.stock_info_global_em, "东方财富", "抓取财经快讯")
    rows = []
    for item in df.to_dict(orient="records"):
        published = _combine(pd.to_datetime(item.get("发布时间"), errors="coerce"))
        if published is None:
            continue
        rows.append(
            {
                "source": "eastmoney",
                "external_id": None,
                "published_at": published,
                "title": str(item.get("标题") or "").strip(),
                "body": str(item.get("摘要") or "").strip(),
                "url": item.get("链接"),
            }
        )
    return _finalize_news(rows)


def _combine(value) -> datetime | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime().replace(tzinfo=None)


def _finalize_news(rows: list[dict]) -> pd.DataFrame:
    columns = ["source", "external_id", "published_at", "title", "body", "url", "content_hash"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows)
    frame["content_hash"] = [
        _hash_news(row.source, row.title, row.published_at) for row in frame.itertuples()
    ]
    return (
        frame.drop_duplicates(subset=["content_hash"])
        .sort_values("published_at")
        .reset_index(drop=True)
    )


# ================================================================ 同花顺备用源
# 东方财富 push2 接口限流很凶（触发后 5~40 分钟不可用），板块域必须有第二数据源。
# 同花顺走 HTML 接口，与 push2 不是同一套限制，两者互为备份。

_THS_BOARD_COLUMNS = {"code": ("code",), "name": ("name",)}

_THS_HIST_COLUMNS = {
    "trade_date": ("日期", "date"),
    "open": ("开盘价", "开盘"),
    "high": ("最高价", "最高"),
    "low": ("最低价", "最低"),
    "close": ("收盘价", "收盘"),
    "volume": ("成交量",),
    "amount": ("成交额",),
}


def fetch_boards_ths(kind: str = "industry") -> pd.DataFrame:
    """同花顺板块清单：industry 约 90 个，concept 约 375 个。"""
    import akshare as ak

    func = ak.stock_board_industry_name_ths if kind == "industry" else ak.stock_board_concept_name_ths
    frame = _call(func, "同花顺", "读取板块目录")
    out = _rename(frame, _THS_BOARD_COLUMNS)
    out["kind"] = kind
    return out[["code", "name", "kind"]].dropna(subset=["name"])


def fetch_board_hist_ths(name: str, kind: str, start: str, end: str) -> pd.DataFrame:
    """同花顺板块指数日线。原始表没有涨跌幅列，用收盘价自己算。"""
    import akshare as ak

    func = (
        ak.stock_board_industry_index_ths
        if kind == "industry"
        else ak.stock_board_concept_index_ths
    )
    frame = _call(func, "同花顺", "读取板块日线", symbol=name, start_date=start, end_date=end)
    empty = pd.DataFrame(
        columns=["trade_date", "open", "high", "low", "close", "pct_chg", "amount", "volume"]
    )
    if frame is None or frame.empty:
        return empty

    out = _rename(frame, _THS_HIST_COLUMNS)
    out["trade_date"] = _to_date(out["trade_date"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        out[column] = _to_num(out[column])
    out = out.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    out["pct_chg"] = out["close"].pct_change() * 100.0
    return out[["trade_date", "open", "high", "low", "close", "pct_chg", "amount", "volume"]]


def fetch_board_flow_ths(kind: str = "industry", period: str = "1d") -> pd.DataFrame:
    """同花顺板块资金流快照（原始单位亿元，这里统一换算成元）。"""
    import akshare as ak

    func = ak.stock_fund_flow_industry if kind == "industry" else ak.stock_fund_flow_concept
    frame = _call(func, "同花顺", "读取板块资金流", symbol={"1d": "即时", "5d": "5日排行", "10d": "10日排行"}[period])
    out = _rename(
        frame,
        {
            "name": ("行业", "概念", "板块"),
            "snapshot_close": ("行业指数", "概念指数"),
            "main_net": ("净额",),
            "inflow": ("流入资金",),
            "outflow": ("流出资金",),
            "close_pct": ("行业-涨跌幅", "阶段涨跌幅"),
        },
    )
    out["name"] = out["name"].astype(str)
    out["close_pct"] = out["close_pct"].astype(str).str.rstrip("%")
    for column in ("snapshot_close", "main_net", "inflow", "outflow", "close_pct"):
        out[column] = _to_num(out[column])
    for column in ("main_net", "inflow", "outflow"):
        out[column] = out[column] * 1e8
    return out.dropna(subset=["name"]).reset_index(drop=True)
