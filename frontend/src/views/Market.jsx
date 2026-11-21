import React, { useState } from "react";
import { Search } from "lucide-react";
import { Table } from "../components.jsx";
import {
  dateText,
  Delta,
  Empty,
  Panel,
  stamp,
  StateNotice,
  yuan,
} from "../desk.jsx";
import { cls, isNil, pctAbs, pts } from "../format.js";
import useResource from "../useResource.js";

const PAGE_SIZE = 25;
const kinds = { industry: "行业", concept: "概念" };
const flowStates = {
  available: "当期数据",
  stale: "最近可得",
  unverified: "日期待核",
  missing: "暂无数据",
};
const forecastStates = {
  available: "可测算",
  partial: "部分期限可测算",
  stale: "缺当期收盘",
  insufficient: "样本或日历不足",
};

export function SourceLink({ source, url }) {
  if (!source) return "—";
  return /^https?:\/\//i.test(url || "") ? (
    <a className="market-source" href={url} target="_blank" rel="noreferrer">
      {source} ↗
    </a>
  ) : (
    source
  );
}

function amount(value) {
  if (isNil(value)) return "—";
  const number = Number(value);
  const scale =
    Math.abs(number) >= 1e8 ? 1e8 : Math.abs(number) >= 1e4 ? 1e4 : 1;
  return `${number > 0 ? "+" : ""}${yuan(number / scale, scale > 1 ? 2 : 0)} ${scale === 1e8 ? "亿" : scale === 1e4 ? "万" : ""}元`;
}

function FlowAmount({ value }) {
  return <span className={cls(value)}>{amount(value)}</span>;
}

function FlowLeaders({ data, method, onPick }) {
  const verified = data.items.filter(
    (r) =>
      r.flow_status === "available" &&
      r.flow_date === data.trade_date &&
      !isNil(r.net_yuan) &&
      (!method || method === r.flow_method),
  );
  if (!verified.length) return null;
  const grouped = new Map();
  for (const r of verified) {
    if (!grouped.has(r.flow_method)) grouped.set(r.flow_method, []);
    grouped.get(r.flow_method).push(r);
  }
  return [...grouped].map(([basis, items]) => {
    const sorted = items.sort((a, b) => b.net_yuan - a.net_yuan);
    const lists = [
      sorted.filter((r) => r.net_yuan > 0).slice(0, 3),
      sorted
        .filter((r) => r.net_yuan < 0)
        .reverse()
        .slice(0, 3),
    ];
    return (
      <section key={basis} className="flow-leaders" aria-label="当期净流向排名">
        {lists.map((subset, i) => (
          <div key={i}>
            <h2>{i ? "净流出居前" : "净流入居前"}</h2>
            {subset.length ? (
              subset.map((row) => (
                <button
                  key={row.board_id}
                  onClick={() => onPick(row.code || row.name)}
                >
                  <span>{row.name}</span>
                  <FlowAmount value={row.net_yuan} />
                </button>
              ))
            ) : (
              <small>当期没有{i ? "净流出" : "净流入"}的板块。</small>
            )}
          </div>
        ))}
        <p>
          {dateText(data.trade_date)} · {basis} · {items.length} 个板块
        </p>
      </section>
    );
  });
}

function Coverage({ data, kind }) {
  const { coverage } = data;
  if (!coverage) return null;
  return (
    <div className="summary-line market-coverage">
      {[
        [`${kinds[kind]}目录`, coverage.total],
        ["当期行情", coverage.quotes],
        ["当期资金流", coverage.flows],
        ["可测算走势", coverage.outlooks],
        ["经理重点跟踪", coverage.tracked],
      ].map(([name, count]) => (
        <div key={name}>
          <span>{name}</span>
          <b>
            {count ?? "—"} <small>个</small>
          </b>
        </div>
      ))}
    </div>
  );
}

function rankValue(row, sort, tradeDate) {
  if (sort === "forecast") {
    const f = row.outlooks?.find((v) => v.horizon_days === 5);
    return f?.prob_up ?? null;
  }
  if (sort === "change")
    return row.quote_date === tradeDate ? row.pct_chg : null;
  if (sort === "fiveFlow")
    return row.flow_status === "available" && row.flow_date === tradeDate
      ? row.net_5d_yuan
      : null;
  if (
    sort === "name" ||
    row.flow_status !== "available" ||
    row.flow_date !== tradeDate ||
    isNil(row.net_yuan)
  )
    return null;
  return sort === "outflow" ? -row.net_yuan : row.net_yuan;
}

function marketRows(items, { search, scope, method, sort, tradeDate }) {
  const nameOrCode = search.trim().toLowerCase();
  return items
    .filter((row) => {
      if (
        nameOrCode &&
        ![row.name, row.code].some((v) =>
          String(v || "")
            .toLowerCase()
            .includes(nameOrCode),
        )
      )
        return false;
      if (method && row.flow_method !== method) return false;
      if (scope === "tracked") return row.tracked;
      if (scope === "flow")
        return (
          row.flow_status === "available" &&
          row.flow_date === tradeDate &&
          !isNil(row.net_yuan)
        );
      if (scope === "outlooks") return row.outlooks?.length;
      return true;
    })
    .sort((a, b) => {
      const x = rankValue(a, sort, tradeDate),
        y = rankValue(b, sort, tradeDate);
      if (isNil(x) !== isNil(y)) return isNil(x) ? 1 : -1;
      if (
        !isNil(x) &&
        ["inflow", "outflow", "fiveFlow"].includes(sort) &&
        a.flow_method !== b.flow_method
      )
        return String(a.flow_method || "").localeCompare(
          b.flow_method || "",
          "zh-CN",
        );
      return (y ?? 0) - (x ?? 0) || a.name.localeCompare(b.name, "zh-CN");
    });
}

const sectorCell = (row, tradeDate) => (
  <div className="cell-stack market-name">
    <b>{row.name}</b>
    <small>
      {row.code || "—"}
      {row.tracked && <span className="tracked-label">经理跟踪</span>}
    </small>
    {row.behavior_label && <small className="market-behavior">假设 · {row.behavior_label}
      {row.behavior_date && row.behavior_date !== tradeDate && ` · ${dateText(row.behavior_date)}`}</small>}
  </div>
);

function MarketTable({ rows, forecasts, tradeDate }) {
  const source = (row, prefix) => (
    <div className="cell-stack">
      <span>
        <SourceLink
          source={row[prefix + "_source"]}
          url={row[prefix + "_url"]}
        />
      </span>
      <small>
        {dateText(row[prefix + "_date"])}
        {row[prefix + "_date"] && row[prefix + "_date"] !== tradeDate
          ? " · 往期"
          : ""}
      </small>
    </div>
  );
  if (forecasts)
    return (
      <Table
        headers={[
          "板块",
          "1日上涨概率",
          "1日预期收益",
          "5日上涨概率",
          "5日预期收益",
          "相似样本（1日 / 5日）",
          "测算依据",
        ]}
        numeric={[1, 2, 3, 4, 5]}
        rows={rows.map((row) => {
          const term = (n) => row.outlooks?.find((f) => f.horizon_days === n);
          const one = term(1),
            five = term(5);
          const probability = (f) =>
            f ? (
              <div className="cell-stack">
                <b>{pctAbs(f.prob_up, 1)}</b>
                <small>至 {dateText(f.target_date)}</small>
              </div>
            ) : (
              <span className="quiet">—</span>
            );
          return [
            sectorCell(row, tradeDate),
            probability(one),
            <Delta value={one?.expected_return} />,
            probability(five),
            <Delta value={five?.expected_return} />,
            `${one?.samples ?? "—"} / ${five?.samples ?? "—"}`,
            <div className="cell-stack">
              <span>
                {row.forecast_note ||
                  forecastStates[row.forecast_status] ||
                  "本期无测算"}
              </span>
              {source(row, "quote")}
            </div>,
          ];
        })}
      />
    );
  return (
    <Table
      headers={[
        "板块",
        "涨跌幅",
        "当日净流入",
        "当日净流入占比",
        "源站5日榜净额 / 占比",
        "资金来源 / 日期",
        "行情来源",
      ]}
      numeric={[1, 2, 3, 4]}
      rows={rows.map((row) => [
        sectorCell(row, tradeDate),
        <span className={cls(row.pct_chg)}>{pts(row.pct_chg)}</span>,
        <div className="cell-stack">
          <FlowAmount value={row.net_yuan} />
          {["stale", "unverified"].includes(row.flow_status) && (
            <small>{flowStates[row.flow_status]}</small>
          )}
        </div>,
        <span className={cls(row.net_pct)}>{pts(row.net_pct)}</span>,
        <div className="cell-stack">
          <FlowAmount value={row.net_5d_yuan} />
          {!isNil(row.net_5d_pct) && <small>{pts(row.net_5d_pct)}</small>}
        </div>,
        <div className="cell-stack">
          {row.flow_source ? source(row, "flow") : "暂无来源"}
        </div>,
        source(row, "quote"),
      ])}
    />
  );
}

export default function Market({ revision, mode = "flows" }) {
  const forecasts = mode === "forecasts";
  const [kind, setKind] = useState("industry");
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState("");
  const [method, setMethod] = useState("");
  const [sort, setSort] = useState(forecasts ? "forecast" : "inflow");
  const [page, setPage] = useState(0);
  const resource = useResource(`/api/agent/market?kind=${kind}`, revision);
  const data = resource.data;
  const methods = [
    ...new Set(
      (data?.items || []).map((row) => row.flow_method).filter(Boolean),
    ),
  ];
  const rows = marketRows(data?.items || [], {
    search,
    scope,
    method,
    sort,
    tradeDate: data?.trade_date,
  });
  const maxPage = Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1);
  const current = Math.min(page, maxPage);
  function change(setter, value) {
    setter(value);
    setPage(0);
  }

  return (
    <div className="page-stack">
      <div className="market-scope">
        <div className="desk-tabs" aria-label="板块类别">
          {Object.entries(kinds).map(([key, name]) => (
            <button
              key={key}
              aria-pressed={key === kind}
              className={key === kind ? "selected" : ""}
              onClick={() => {
                change(setKind, key);
                setMethod("");
              }}
            >
              {name}板块
            </button>
          ))}
        </div>
        {data && (
          <small className="quiet">
            最近收盘交易日 {dateText(data.trade_date)} · 截至{" "}
            {stamp(data.as_of)}
          </small>
        )}
      </div>
      <StateNotice resource={resource} />
      {data && (
        <>
          <Coverage data={data} kind={kind} />
          {!forecasts && (
            <FlowLeaders
              data={data}
              method={method}
              onPick={(name) => {
                change(setSearch, name);
                setScope("");
              }}
            />
          )}
          <Panel
            title={
              forecasts ? `${kinds[kind]}走势测算` : `${kinds[kind]}资金流向`
            }
            extra={<small className="quiet">共 {rows.length} 个板块</small>}
          >
            {forecasts && (
              <p className="market-description">
                按各板块可用的历史行情计算 1 日和 5
                日走势。经理正式发布的板块判断和到期成绩可在「板块正式预测」查看。
              </p>
            )}
            {!forecasts && (
              <p className="market-description">
                {data.coverage?.flows === 0
                  ? "最近收盘交易日暂无可核实的板块资金流。表中往期数据列出实际日期。"
                  : "资金流按来源口径分别排序，正值为净流入，负值为净流出。"}
              </p>
            )}
            <div className="toolbar market-toolbar">
              <label className="sector-search">
                <Search size={15} />
                <input
                  value={search}
                  type="search"
                  aria-label="搜索板块名称或代码"
                  placeholder="搜索名称或代码"
                  onChange={(e) => change(setSearch, e.target.value)}
                />
              </label>
              <label>
                范围
                <select
                  value={scope}
                  onChange={(e) => change(setScope, e.target.value)}
                >
                  <option value="">全部板块</option>
                  <option value="tracked">经理重点跟踪</option>
                  {forecasts ? (
                    <option value="outlooks">有走势测算</option>
                  ) : (
                    <option value="flow">当期有资金流</option>
                  )}
                </select>
              </label>
              {!forecasts && methods.length > 1 && (
                <label>
                  资金口径
                  <select
                    value={method}
                    onChange={(e) => change(setMethod, e.target.value)}
                  >
                    <option value="">全部口径</option>
                    {methods.map((m) => (
                      <option key={m}>{m}</option>
                    ))}
                  </select>
                </label>
              )}
              <label>
                排序
                <select
                  value={sort}
                  onChange={(e) => change(setSort, e.target.value)}
                >
                  {forecasts ? (
                    <option value="forecast">5日上涨概率</option>
                  ) : (
                    <>
                      <option value="inflow">当期净流入从高到低</option>
                      <option value="outflow">当期净流出从高到低</option>
                      <option value="fiveFlow">5日榜净流入从高到低</option>
                      <option value="change">当期涨幅从高到低</option>
                    </>
                  )}
                  <option value="name">名称</option>
                </select>
              </label>
            </div>
            {rows.length ? (
              <MarketTable
                forecasts={forecasts}
                rows={rows.slice(
                  current * PAGE_SIZE,
                  (current + 1) * PAGE_SIZE,
                )}
                tradeDate={data.trade_date}
              />
            ) : (
              <Empty>
                {data.items?.length
                  ? "没有符合筛选条件的板块。"
                  : "该类别的板块目录尚未入库。"}
              </Empty>
            )}
            {rows.length > 0 && (
              <div className="pager">
                <span>
                  {current * PAGE_SIZE + 1}–
                  {Math.min(rows.length, (current + 1) * PAGE_SIZE)} /{" "}
                  {rows.length}
                </span>
                <button
                  disabled={!current}
                  onClick={() => setPage(current - 1)}
                >
                  上一页
                </button>
                <button
                  disabled={current >= maxPage}
                  onClick={() => setPage(current + 1)}
                >
                  下一页
                </button>
              </div>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}
