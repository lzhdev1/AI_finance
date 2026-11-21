"""盘中实时数据：板块/指数行情、仓位估值、30 分钟走势预测与结算。

为什么需要这一层：
场外基金一天只有一个净值，但决策必须在 15:00 前做出。要"挣第二天的钱"，
14:30 就必须知道"现在大概什么位置"——只能靠盘中行情估算，而不是等晚上净值。

数据源：
* 板块实时涨跌 —— 东方财富 push2（免费、无需 key，一次请求拿全市场板块）
* 指数实时 —— 新浪 hq.sinajs.cn（免费、极稳，GBK 编码）

诚实说明：
* 对**指数型基金**，用其跟踪板块的实时涨跌估算当日涨跌，误差通常在 0.1~0.3 个百分点；
* 对**主动管理型基金**，这个估算可能偏差很大（基金经理的持仓我们看不到），
  所以只在决策卡上标注"估算"，绝不当成真实净值写进账本。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pandas as pd

from . import board_spots, db, usage

log = logging.getLogger(__name__)

EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
SINA_HQ = "https://hq.sinajs.cn/list="

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 沪深300 + 上证 + 深证 + 创业板，用于判断大盘状态与顶部行情条
INDEX_CODES = {
    "000001": "sh000001",
    "399001": "sz399001",
    "399006": "sz399006",
    "000300": "sh000300",
}
INDEX_NAMES = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指", "000300": "沪深300"}

_QUOTE_FEEDS = {
    "push2.eastmoney.com": ("东方财富", "读取板块盘中行情"),
    "vip.stock.finance.sina.com.cn": ("新浪财经", "读取行业盘中行情"),
    "hq.sinajs.cn": ("新浪财经", "读取指数盘中行情"),
    "q.10jqka.com.cn": ("同花顺", "核对概念板块行情代码"),
    "d.10jqka.com.cn": ("同花顺", "读取板块分时行情"),
}


def _get(url: str, referer: str, encoding: str = "utf-8", timeout: int = 10) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": referer, "Accept": "*/*"},
    )
    provider, operation = _QUOTE_FEEDS.get(urlsplit(url).hostname, ("公开行情源", "读取盘中行情"))
    with usage.attempt(kind="data", provider=provider, operation=operation, detail="行情 HTTP GET 请求。"):
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode(encoding, errors="replace")


def fetch_board_spot(page_size: int = 100, max_pages: int = 8) -> pd.DataFrame:
    """盘中全市场板块实时行情（行业 + 概念）。

    一次请求带齐：涨跌幅、成交额、成交量、换手率、主力净流入、主力净占比、涨跌家数。
    这些都是"放量缩量 / 资金动向"分析的最小集合，少一个就得再抓一遍。
    """
    rows: list[dict] = []
    for fs, kind in (("m:90+t:2", "industry"), ("m:90+t:3", "concept")):
        for page in range(1, max_pages + 1):
            query = (
                f"?pn={page}&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid=f3"
                f"&fs={fs}&fields=f2,f3,f5,f6,f8,f12,f14,f62,f104,f105,f124,f184"
            )
            payload = None
            for attempt in range(2):  # push2 偶尔断连，重试一次再放弃这一页
                try:
                    payload = json.loads(_get(EM_CLIST + query, referer="https://quote.eastmoney.com/"))
                    break
                except Exception as exc:  # 限流/网络问题不该让整轮决策失败
                    if attempt == 1:
                        log.warning("板块实时抓取失败（%s 第 %s 页）：%s", kind, page, exc)
                    else:
                        time.sleep(0.6)
            if payload is None:
                break
            data = payload.get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            for item in diff:
                pct = item.get("f3")
                if pct in (None, "-", ""):
                    continue
                rows.append(
                    {
                        "board_name": item.get("f14"),
                        "pct_chg": float(pct),
                        "code": item.get("f12"),
                        "source": "eastmoney",
                        "source_at": _eastmoney_time(item.get("f124")),
                        "kind": kind,
                        "amount": _num_or_none(item.get("f6")),
                        "volume": _num_or_none(item.get("f5")),
                        "turnover_rate": _num_or_none(item.get("f8")),
                        "main_net": _num_or_none(item.get("f62")),
                        "main_ratio": _num_or_none(item.get("f184")),
                        "up_count": _int_or_none(item.get("f104")),
                        "down_count": _int_or_none(item.get("f105")),
                    }
                )
            if len(diff) < page_size:
                break
            time.sleep(0.25)  # 温和节奏，避免被 push2 掐断

    columns = [
        "board_name", "pct_chg", "code", "kind",
        "amount", "volume", "turnover_rate", "main_net", "main_ratio",
        "up_count", "down_count", "source", "source_at",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        # 东财被限流（IP 级）时整轮都拿不到，退回新浪：只有行业、没有资金流字段，
        # 但涨跌和成交额还在，量比照样能算。降级要留痕，不能假装数据完整。
        log.warning("东财板块行情不可用，改用新浪行业板块（无主力资金字段）")
        return fetch_board_spot_sina()
    return frame.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)


def _eastmoney_time(epoch):
    try:
        value = int(epoch)
        return datetime.fromtimestamp(value, timezone.utc) if value > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


SINA_SECTOR = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def fetch_board_spot_sina() -> pd.DataFrame:
    """新浪行业板块：code,名称,公司家数,均价,涨跌额,涨跌幅,成交量,成交额,领涨股...

    只覆盖 49 个行业板块、没有概念板块，也没有主力资金。是"能用但更粗"的降级源。
    """
    columns = [
        "board_name", "pct_chg", "code", "kind",
        "amount", "volume", "turnover_rate", "main_net", "main_ratio",
        "up_count", "down_count", "source", "source_at",
    ]
    try:
        import re

        text = _get(SINA_SECTOR, referer="https://finance.sina.com.cn", encoding="gbk")
        match = re.search(r"=\s*(\{.*\})", text, re.S)
        if not match:
            return pd.DataFrame(columns=columns)
        payload = json.loads(match.group(1))
    except Exception as exc:  # noqa: BLE001
        log.warning("新浪行业板块也失败：%s", exc)
        return pd.DataFrame(columns=columns)

    rows = []
    for code, value in payload.items():
        parts = str(value).split(",")
        if len(parts) < 8:
            continue
        try:
            rows.append(
                {
                    "board_name": parts[1],
                    "pct_chg": float(parts[5]),
                    "code": code,
                    "source": "sina",
                    "source_at": None,  # This public feed has no vendor date or minute.
                    "kind": "industry",
                    "amount": float(parts[7]),
                    "volume": float(parts[6]),
                    "turnover_rate": None,
                    "main_net": None,
                    "main_ratio": None,
                    "up_count": None,
                    "down_count": None,
                }
            )
        except (ValueError, IndexError):
            continue
    return pd.DataFrame(rows, columns=columns).reset_index(drop=True)


def fetch_index_quotes() -> dict[str, dict]:
    """指数实时行情（涨跌幅 + 点位）。新浪接口返回 GBK，必须按 gbk 解码。"""
    url = SINA_HQ + ",".join(INDEX_CODES.values())
    try:
        text = _get(url, referer="https://finance.sina.com.cn", encoding="gbk")
    except Exception as exc:
        log.warning("指数实时抓取失败：%s", exc)
        return {}

    reverse = {value: key for key, value in INDEX_CODES.items()}
    result: dict[str, dict] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        _, _, payload = line.partition("=")
        fields = payload.strip().strip('";').split(",")
        if len(fields) < 4:
            continue
        symbol = line.split("hq_str_")[-1].split("=")[0]
        code = reverse.get(symbol)
        if code is None:
            continue
        try:
            prev_close = float(fields[2])
            current = float(fields[3])
            if prev_close > 0 and current > 0:
                result[code] = {
                    "name": INDEX_NAMES.get(code, code),
                    "price": round(current, 2),
                    "pct": (current / prev_close - 1.0) * 100.0,
                }
        except (ValueError, IndexError):
            continue
    return result


def fetch_index_spot() -> dict[str, float]:
    """只要涨跌幅的旧接口，保留给已有调用方。"""
    return {code: item["pct"] for code, item in fetch_index_quotes().items()}


# ---------------------------------------------------------------- 落库

def _num_or_none(value) -> float | None:
    """东财缺值会给 '-' / '' / None，统一成 None，别让 float() 炸掉整轮抓取。"""
    if value in (None, "-", "", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> int | None:
    number = _num_or_none(value)
    return int(number) if number is not None else None


def save_board_spot(frame: pd.DataFrame, ts: datetime | None = None) -> int:
    if frame.empty:
        return 0
    ts = ts or datetime.now().astimezone().replace(microsecond=0)
    board_spots.save(frame, ts)
    payload = [
        (
            ts,
            row.board_name,
            float(row.pct_chg),
            row.kind,
            getattr(row, "amount", None),
            getattr(row, "volume", None),
            getattr(row, "turnover_rate", None),
            getattr(row, "main_net", None),
            getattr(row, "main_ratio", None),
            getattr(row, "up_count", None),
            getattr(row, "down_count", None),
        )
        for row in frame.drop_duplicates("board_name").itertuples(index=False)
    ]
    db.execute_many(
        f"""
        INSERT INTO {db.qualified('board_intraday')}
            (ts, board_name, pct_chg, kind,
             amount, volume, turnover_rate, main_net, main_ratio, up_count, down_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts, board_name) DO UPDATE SET
            pct_chg = EXCLUDED.pct_chg,
            amount = EXCLUDED.amount,
            volume = EXCLUDED.volume,
            turnover_rate = EXCLUDED.turnover_rate,
            main_net = EXCLUDED.main_net,
            main_ratio = EXCLUDED.main_ratio,
            up_count = EXCLUDED.up_count,
            down_count = EXCLUDED.down_count
        """,
        payload,
    )
    return len(payload)


def save_index_spot(spots: dict, ts: datetime | None = None) -> int:
    """spots 既接受 {code: pct}，也接受 {code: {pct, price, name}}。"""
    if not spots:
        return 0
    ts = ts or datetime.now().astimezone().replace(microsecond=0)
    rows = []
    for code, value in spots.items():
        if isinstance(value, dict):
            rows.append((ts, code, float(value["pct"]), value.get("price")))
        else:
            rows.append((ts, code, float(value), None))
    db.execute_many(
        f"""
        INSERT INTO {db.qualified('index_intraday')} (ts, index_code, pct_chg, price)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ts, index_code) DO UPDATE
        SET pct_chg = EXCLUDED.pct_chg, price = EXCLUDED.price
        """,
        rows,
    )
    return len(rows)


def snapshot() -> dict:
    """一次盘中快照：板块 + 指数，同时刷新各仓估值。"""
    as_of = datetime.now(board_spots.SH)
    em = db.query_one(f"SELECT 1 FROM {db.qualified('board')} WHERE is_active AND source='eastmoney' LIMIT 1")
    boards = fetch_board_spot() if em else pd.DataFrame()
    ths = board_spots.fetch_ths(as_of, _get)
    indexes = fetch_index_quotes()
    ts = datetime.now().astimezone()
    save_board_spot(boards, ts)
    board_spots.save(ths, ts)
    usable = board_spots.available(ts)
    n_index = save_index_spot(indexes, ts)
    pods = estimate_pods(ts, observations=usable)
    return {
        "ts": ts.isoformat(),
        "boards": len(usable),
        "raw_boards": len(boards) + len(ths),
        "indexes": n_index,
        "pods": pods,
        "hs300": indexes.get("000300", {}).get("pct"),
    }


# ---------------------------------------------------------------- 仓位估值

def board_pct_map() -> dict[str, float]:
    """最近一次快照里的板块涨跌幅。"""
    rows = db.query(
        f"""
        SELECT DISTINCT ON (board_name) board_name, pct_chg
        FROM {db.qualified('board_intraday')}
        ORDER BY board_name, ts DESC
        """
    )
    return {row["board_name"]: float(row["pct_chg"]) for row in rows}


def estimate_pods(ts: datetime | None = None, *, observations=None) -> list[dict]:
    """按持仓权重估算每个仓的当日涨跌，写入 pod_intraday。

    估算方式：仓内每只基金 → 它的板块 → 板块今日实时涨跌，按权重加权。
    没有板块映射的基金（如债基）按 0 处理，并单独标注出来——不假装能估准。
    """
    ts = ts or datetime.now().astimezone().replace(microsecond=0)
    if observations is None:
        observations = board_spots.available(ts)
    if not observations:
        return []
    results: list[dict] = []
    for pod in db.query(
        f"SELECT pod_id, pod_no, name FROM {db.qualified('pod')} WHERE status = 'active' ORDER BY pod_no"
    ):
        rows = db.query(
            f"""
            SELECT DISTINCT ON (i.code) i.code, i.name, bd.board_id, bd.name AS board_name, pos.weight
            FROM {db.qualified('pod_position')} pos
            JOIN {db.qualified('instrument')} i USING (instrument_id)
            LEFT JOIN {db.qualified('instrument_board')} ib USING (instrument_id)
            LEFT JOIN {db.qualified('board')} bd USING (board_id)
            WHERE pos.pod_id = %s
              AND pos.trade_date = (SELECT max(trade_date) FROM {db.qualified('pod_nav')} WHERE pod_id = %s)
            ORDER BY i.code, ib.weight DESC
            """,
            [pod["pod_id"], pod["pod_id"]],
        )
        if not rows:
            continue
        total = 0.0
        covered = 0.0
        detail = []
        for row in rows:
            weight = float(row["weight"])
            board = row["board_name"]
            pct = observations.get(row["board_id"], {}).get("pct_chg")
            if pct is not None:
                total += weight * pct
                covered += weight
            detail.append({"code": row["code"], "board": board, "weight": weight, "pct": pct})

        # 没被板块覆盖的部分（债基、未映射基金）按 0 处理：宁可低估，不瞎猜
        results.append(
            {
                "pod_no": pod["pod_no"],
                "name": pod["name"],
                "est_pct": total,
                "coverage": covered,
                "detail": detail,
            }
        )
        db.execute(
            f"""
            INSERT INTO {db.qualified('pod_intraday')} (pod_id, ts, est_pct, coverage, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (pod_id, ts) DO UPDATE
              SET est_pct = EXCLUDED.est_pct, coverage = EXCLUDED.coverage, detail = EXCLUDED.detail
            """,
            [pod["pod_id"], ts, total, covered, json.dumps(detail, ensure_ascii=False)],
        )
    return results


# ---------------------------------------------------------------- 30 分钟走势

def forecast_30m(horizon_min: int = 30) -> list[dict]:
    """用盘中已采样的估值序列外推未来 30 分钟。

    方法很朴素，而且我在页面上会直接写出来：取最近 20 分钟的斜率做线性外推，
    并用这段窗口的波动率给一个上下界。它预测不了拐点，只能延续趋势——
    所以它的价值不在于"准"，而在于让你看到"如果不反转，收盘大概在哪"。
    """
    now = datetime.now().astimezone().replace(microsecond=0)
    made: list[dict] = []
    for pod in db.query(
        f"SELECT pod_id, pod_no, name FROM {db.qualified('pod')} WHERE status = 'active' ORDER BY pod_no"
    ):
        series = db.query(
            f"""
            SELECT ts, est_pct FROM {db.qualified('pod_intraday')}
            WHERE pod_id = %s AND ts >= now() - interval '90 minutes'
            ORDER BY ts
            """,
            [pod["pod_id"]],
        )
        if len(series) < 4:
            continue
        xs = [(row["ts"] - series[0]["ts"]).total_seconds() / 60.0 for row in series]
        ys = [float(row["est_pct"]) for row in series]
        window = min(len(xs), 10)
        xs_w, ys_w = xs[-window:], ys[-window:]
        slope = _slope(xs_w, ys_w)
        vol = _stdev([y - slope * x for x, y in zip(xs_w, ys_w)])

        last_y = ys[-1]
        path = [
            {"offset_min": step, "pct": last_y + slope * step}
            for step in range(0, horizon_min + 1, 5)
        ]
        predicted = last_y + slope * horizon_min
        band = max(vol * 1.5, 0.02)

        db.execute(
            f"""
            INSERT INTO {db.qualified('pod_forecast_30m')}
                (pod_id, trade_date, made_at, horizon_min, start_pct, predicted_pct, band_pct, slope_per_min, path)
            VALUES (%s, current_date, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pod_id, trade_date) DO UPDATE
              SET made_at = EXCLUDED.made_at, start_pct = EXCLUDED.start_pct,
                  predicted_pct = EXCLUDED.predicted_pct, band_pct = EXCLUDED.band_pct,
                  slope_per_min = EXCLUDED.slope_per_min, path = EXCLUDED.path
            """,
            [
                pod["pod_id"], now, horizon_min, last_y, predicted, band, slope,
                json.dumps(path, ensure_ascii=False),
            ],
        )
        made.append({"pod_no": pod["pod_no"], "start": last_y, "predicted": predicted})
    return made


def _slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def settle_forecast() -> int:
    """收盘后把 30 分钟预测和实际走势对上，算误差。"""
    rows = db.query(
        f"""
        SELECT f.pod_id, f.trade_date, f.made_at, f.horizon_min, f.predicted_pct
        FROM {db.qualified('pod_forecast_30m')} f
        WHERE f.actual_pct IS NULL AND f.trade_date <= current_date
        """
    )
    settled = 0
    for row in rows:
        target = row["made_at"] + timedelta(minutes=row["horizon_min"])
        hit = db.query_one(
            f"""
            SELECT est_pct, ts FROM {db.qualified('pod_intraday')}
            WHERE pod_id = %s AND ts >= %s AND ts <= %s
            ORDER BY ts LIMIT 1
            """,
            [row["pod_id"], target - timedelta(minutes=3), target + timedelta(minutes=3)],
        )
        if not hit:
            continue
        actual = float(hit["est_pct"])
        db.execute(
            f"""
            UPDATE {db.qualified('pod_forecast_30m')}
            SET actual_pct = %s, error_pct = %s, settled_at = now()
            WHERE pod_id = %s AND trade_date = %s
            """,
            [actual, actual - float(row["predicted_pct"]), row["pod_id"], row["trade_date"]],
        )
        settled += 1
    return settled
