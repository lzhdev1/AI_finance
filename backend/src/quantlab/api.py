"""QuantLab HTTP API：前端唯一的数据入口。

设计原则：
* 只做序列化，不算业务——口径全在 ``quantlab.queries``；
* 每个响应都是稳定结构（键不随数据缺失而消失），前端不用写防御性判断；
* 出错就 500 + 明确信息，不返回"看起来正常但其实是空的"数据。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from quantlab import db, replay_archive, replay_comparison, station

log = logging.getLogger("quantlab.api")

app = FastAPI(title="QuantLab API", version="2.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

# 开发期前端跑在 5173/3000，生产由 nginx 反代同源；放开来源省得来回改
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.wait_for_db()


@app.get("/api/health")
def health() -> dict:
    if not db.healthcheck():
        raise HTTPException(503, "数据库未就绪")
    return {"status": "ok", "db": True, "ts": station.now().isoformat()}


@app.get("/api/agent/dashboard")
def agent_dashboard(report_id: int | None = Query(default=None, ge=1)) -> dict:
    data = station.dashboard(report_id)
    if report_id is not None and not data["report"]:
        raise HTTPException(404, "研究报告不存在")
    return data


@app.get("/api/agent/market")
def agent_market(kind: Literal["industry", "concept"] = "industry") -> dict:
    from .market import market_screen

    return market_screen(kind)


@app.get("/api/agent/history")
def agent_history(kind: Literal["orders", "runs", "events", "forecasts", "fund_forecasts", "decisions", "reports"],
                  offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=200),
                  horizon: int | None = None, status: Literal["pending", "scored", "insufficient"] | None = None) -> dict:
    if status == "insufficient" and kind != "fund_forecasts":
        raise HTTPException(422, "该筛选仅用于基金净值预测")
    if horizon is not None and horizon not in (1, 5):
        raise HTTPException(422, "支持的预测期限为 1 或 5 个交易日")
    return station.history(kind, offset, limit, horizon, status)


@app.get("/api/agent/replay")
def replay_summary(version: Literal["v1", "v2"] = "v1") -> dict:
    return replay_archive.summary(version)


@app.get("/api/agent/replay/days")
def replay_days(action: Literal["all", "open", "add", "reduce", "rebalance", "hold", "close"] = "all",
                offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=125),
                version: Literal["v1", "v2"] = "v1", day: dt.date | None = None) -> dict:
    return replay_archive.days_page(action, offset, limit, version, day)


@app.get("/api/agent/replay/comparison")
def replay_versions() -> dict:
    return replay_comparison.comparison()


@app.get("/api/agent/replay/attribution")
def replay_attribution(version: Literal["v1", "v2"] = "v1") -> dict:
    return replay_comparison.attribution(version)


@app.post("/api/agent/research", status_code=202)
def request_research(request: Request) -> dict:
    # 浏览器仅接受工作台同源请求，手动触发会消耗配置的模型额度。
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "请从本地工作台发起研究")
    if not station.scheduler_state()["scheduler_online"]:
        raise HTTPException(503, "调度器离线，请检查 scheduler 容器")
    try:
        return station.queue_research()
    except station.ResearchBusy as exc:
        raise HTTPException(409, f"已有研究任务 #{exc.args[0]} 正在运行") from exc
