import React, { useState } from "react";
import { Table } from "./components.jsx";
import { dateText, Delta, Empty, label, StateNotice, Tag } from "./desk.jsx";
import { forecastDirection, pctAbs } from "./format.js";
import useResource from "./useResource.js";

export function HistoryPage({ kind, revision = 0, params = {}, children }) {
  const [page, setPage] = useState(0);
  const limit = 40;
  const query = new URLSearchParams({ kind, offset: page * limit, limit });
  for (const [name, value] of Object.entries(params)) {
    if (value) query.set(name, value);
  }
  const resource = useResource(`/api/agent/history?${query}`, revision);
  return (
    <>
      <StateNotice resource={resource} />
      {resource.data && children(resource.data.items)}
      {(page > 0 || resource.data?.has_more) && (
        <div className="pager">
          <span>第 {page + 1} 页</span>
          <button disabled={!page} onClick={() => setPage((p) => p - 1)}>
            上一页
          </button>
          <button
            disabled={!resource.data?.has_more}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}
export function ForecastTable({ rows = [] }) {
  if (!rows.length) return <Empty>当前记录中没有板块预测。</Empty>;
  return (
    <Table
      headers={[
        "板块 / 核验标的",
        "基准日",
        "预测期 / 目标日",
        "收盘方向 / 上涨概率",
        "预期收益",
        "相似样本",
        "置信度",
        "实际收益",
        "验证结果",
      ]}
      numeric={[3, 4, 5, 7]}
      rows={rows.map((row) => [
        <div className="cell-stack">
          <b>{row.board}</b>
          <small>
            {row.proxy_code
              ? `基金净值代理 ${row.proxy_code}`
              : "板块指数收盘价"}
          </small>
        </div>,
        dateText(row.base_date),
        <div className="cell-stack">
          {row.horizon_days} 个交易日<small>{dateText(row.target_date)}</small>
        </div>,
        <div className="cell-stack"><b>{forecastDirection(row.prob_up)}</b><small>{pctAbs(row.prob_up, 1)}</small></div>,
        <Delta value={row.expected_return} />,
        row.samples ?? "—",
        label(row.confidence),
        <Delta value={row.realized_return} />,
        <Tag value={row.status}>
          {row.status !== "scored"
            ? label(row.status)
            : row.hit == null
              ? "已验证 · 方向中性"
              : row.hit
                ? "方向命中"
                : "方向未命中"}
        </Tag>,
      ])}
    />
  );
}
