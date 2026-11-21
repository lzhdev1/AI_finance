import React, { useState } from "react";
import { ChevronDown, Clock3, Fingerprint, ListFilter } from "lucide-react";
import { Table } from "../components.jsx";
import { pctAbs, num } from "../format.js";
import {
  dateText,
  Empty,
  label,
  Panel,
  stamp,
  Tag,
  TextBlock,
  yuan,
} from "../desk.jsx";
import { ForecastTable, HistoryPage } from "../Archive.jsx";
import { Events, Performance } from "./Overview.jsx";
import { MemoryTable } from "./Research.jsx";
import Market from "./Market.jsx";
import FundForecasts from "./FundForecasts.jsx";

function Tabs({ options, value, onChange, label: name }) {
  return (
    <div className="desk-tabs" aria-label={name}>
      {options.map(([id, title]) => (
        <button
          key={id}
          aria-pressed={value === id}
          className={value === id ? "selected" : ""}
          onClick={() => onChange(id)}
        >
          {title}
        </button>
      ))}
    </div>
  );
}
function StatsLine({ items }) {
  return (
    <div className="summary-line">
      {items.map(([name, value]) => (
        <div key={name}>
          <span>{name}</span>
          <b>{value}</b>
        </div>
      ))}
    </div>
  );
}
export function Forecasts({ revision }) {
  const [kind, setKind] = useState(() => {
    const parameter = new URLSearchParams(location.search).get("pred_tab");
    return parameter === "funds" ? "funds" : parameter === "published" ? "sector" : "market";
  });
  function selectKind(next) {
    const url = new URL(window.location.href);
    if (next === "sector") url.searchParams.set("pred_tab", "published");
    else if (next === "funds") url.searchParams.set("pred_tab", "funds");
    else url.searchParams.delete("pred_tab");
    window.history.replaceState({}, "", url);
    if (next !== "funds" && status === "insufficient") setStatus("");
    setKind(next);
  }
  const [status, setStatus] = useState("");
  const [horizon, setHorizon] = useState("");
  const horizons = [1, 5];
  return (
    <div className="page-stack">
      <Tabs
        label="预测类型"
        value={kind}
        onChange={selectKind}
        options={[
          ["market", "全市场测算"],
          ["sector", "板块正式预测"],
          ["funds", "基金净值预测"],
        ]}
      />
      {kind === "market" ? (
        <Market revision={revision} mode="forecasts" />
      ) : (
        <>
          <div className="toolbar">
            <label>
              <ListFilter size={15} /> 预测期限
              <select
                value={horizon}
                onChange={(e) => setHorizon(e.target.value)}
              >
                <option value="">全部期限</option>
                {horizons.map((n) => (
                  <option key={n} value={n}>
                    {n} 个交易日
                  </option>
                ))}
              </select>
            </label>
            <label>
              验证状态
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
              >
                <option value="">全部状态</option>
                <option value="pending">待验证</option>
                <option value="scored">已验证</option>
                {kind === "funds" && <option value="insufficient">未形成预测</option>}
              </select>
            </label>
          </div>
          <Panel
            title={kind === "funds" ? "关注基金的单位净值预测" : "板块预测与实际走势"}
            extra={<small className="quiet">按预测发布时间排序</small>}
          >
            <HistoryPage
              key={`${kind}-${horizon}-${status}`}
              kind={kind === "funds" ? "fund_forecasts" : "forecasts"}
              revision={revision}
              params={{ horizon, status }}
            >
              {(rows) => {
                const scored = rows.filter((row) => row.status === "scored");
                const directed = scored.filter((row) => row.hit != null);
                const insufficient = rows.filter((row) => row.status === "insufficient").length;
                return (
                  <>
                    <StatsLine
                      items={[
                        ["本页正式预测", rows.length - insufficient],
                        [
                          "待验证",
                          rows.filter((r) => r.status === "pending").length,
                        ],
                        ["已验证", scored.length],
                        ...(insufficient ? [["未形成预测", insufficient]] : []),
                        [
                          "本页方向命中率",
                          directed.length
                            ? pctAbs(
                                directed.filter((r) => r.hit).length /
                                  directed.length,
                                1,
                              )
                            : "—",
                        ],
                      ]}
                    />
                    {kind === "funds" ? <FundForecasts rows={rows} /> : <ForecastTable rows={rows} />}
                  </>
                );
              }}
            </HistoryPage>
          </Panel>
        </>
      )}
    </div>
  );
}

function OrdersTable({ rows }) {
  return rows.length ? (
    <Table
      headers={[
        "成交日",
        "基金",
        "方向",
        "金额（元）",
        "成交净值",
        "份额",
        "费用（元）",
        "指令 / 原因",
      ]}
      numeric={[3, 4, 5, 6]}
      rows={rows.map((row) => [
        dateText(row.execute_on),
        <div className="cell-stack">
          <b>{row.name}</b>
          <small>{row.code}</small>
        </div>,
        <Tag value={row.side}>{label(row.side)}</Tag>,
        yuan(row.amount),
        num(row.nav, 4),
        yuan(row.shares, 4),
        yuan(row.fee),
        <div className="cell-stack">
          {dateText(row.decided_on)}
          <small>{row.reason || "—"}</small>
        </div>,
      ])}
    />
  ) : (
    <Empty>尚无已按净值确认的模拟成交。</Empty>
  );
}
function Decisions({ items }) {
  if (!items?.length) return <Empty>尚无经理组合的决策指令。</Empty>;
  return (
    <div className="watch-list">
      {items.map((item, i) => (
        <details className="decision" key={`${item.trade_date}-${i}`}>
          <summary>
            <div>
              <small>
                {dateText(item.trade_date)} · {label(item.action)}
              </small>
              <strong>{item.summary || label(item.action)}</strong>
            </div>
            <Tag
              value={
                item.payload?.settlement?.status ||
                (item.executed_at ? "settled" : "pending")
              }
            >
              {label(
                item.payload?.settlement?.status ||
                  (item.executed_at ? "settled" : "pending"),
              ).replace("待验证", "待结算")}
            </Tag>
            <ChevronDown size={16} />
          </summary>
          <div className="decision-detail">
            <TextBlock title="决策依据">{item.rationale}</TextBlock>
            {item.payload?.research_id && (
              <a
                className="report-link"
                href={`?view=research&report=${item.payload.research_id}`}
              >
                查看关联研究报告 #{item.payload.research_id} ↗
              </a>
            )}
            <TextBlock title="结算说明">
              {item.payload?.settlement?.reason}
            </TextBlock>
            {item.payload?.risk_checks?.length > 0 && (
              <TextBlock title="风控检查">
                <ul>
                  {item.payload.risk_checks.map((note, j) => (
                    <li key={j}>{note}</li>
                  ))}
                </ul>
              </TextBlock>
            )}
            {item.executed_at && (
              <small className="quiet">
                确认时间 {stamp(item.executed_at)}
              </small>
            )}
            <TextBlock title="冻结的模拟指令">
              {(item.payload?.instructions || item.payload?.pending)?.length ? (
                <Table
                  headers={["方向", "基金代码", "申购金额（元）", "赎回份额"]}
                  numeric={[2, 3]}
                  rows={(item.payload.instructions || item.payload.pending).map(
                    (order) => [
                      label(order.side),
                      order.code,
                      order.side === "buy" ? yuan(order.amount) : "—",
                      order.side === "sell" ? yuan(order.shares, 6) : "—",
                    ],
                  )}
                />
              ) : (
                "本条记录没有待执行的申赎指令。"
              )}
            </TextBlock>
          </div>
        </details>
      ))}
    </div>
  );
}
export function Ledger({ data, revision }) {
  const [tab, setTab] = useState("account");
  const manager = data.manager;
  return (
    <div className="page-stack">
      <Tabs
        value={tab}
        onChange={setTab}
        label="账本视图"
        options={[
          ["account", "持仓与净值"],
          ["orders", "成交流水"],
        ]}
      />
      {tab === "account" &&
        (manager ? (
          <>
            <StatsLine
              items={[
                ["初始资金（元）", yuan(manager.initial_capital)],
                ["当前资产（元）", yuan(manager.total_value)],
                ["基金市值（元）", yuan(manager.market_value)],
                ["账面现金（元）", yuan(manager.cash)],
              ]}
            />
            <Panel
              title="基金持仓"
              extra={
                <small className="quiet">
                  {manager.name} · {dateText(manager.last_date)}
                </small>
              }
            >
              {manager.holdings?.length ? (
                <Table
                  headers={[
                    "基金",
                    "板块",
                    "持仓比例",
                    "市值（元）",
                    "平均成本",
                    "最新净值",
                  ]}
                  numeric={[2, 3, 4, 5]}
                  rows={manager.holdings.map((row) => [
                    <div className="cell-stack">
                      <b>{row.name}</b>
                      <small>{row.code}</small>
                    </div>,
                    row.boards || "—",
                    pctAbs(row.weight, 1),
                    yuan(row.market_value),
                    num(row.avg_cost, 4),
                    num(row.last_nav, 4),
                  ])}
                />
              ) : (
                <Empty>经理组合当前没有基金持仓。</Empty>
              )}
            </Panel>
            <Performance manager={manager} />
          </>
        ) : (
          <Panel title="经理组合">
            <Empty>尚无经理组合账户，等待首次模拟决策。</Empty>
          </Panel>
        ))}
      {tab === "orders" && (
        <Panel title="经理账户已确认成交">
          <HistoryPage kind="orders" revision={revision}>
            {(rows) => <OrdersTable rows={rows} />}
          </HistoryPage>
        </Panel>
      )}
    </div>
  );
}

export function DecisionHistory({ revision }) {
  return (
    <Panel title="经理决策指令">
      <HistoryPage kind="decisions" revision={revision}>
        {(rows) => <Decisions items={rows} />}
      </HistoryPage>
    </Panel>
  );
}

export function Memory({ data }) {
  return (
    <Panel
      title="已验证的板块经验"
      extra={<Fingerprint size={17} className="quiet" />}
    >
      <MemoryTable items={data.memories} />
    </Panel>
  );
}

function RunList({ rows }) {
  return rows.length ? (
    <div className="watch-list">
      {rows.map((row) => (
        <details className="decision" key={row.run_id}>
          <summary>
            <div>
              <small>
                #{row.run_id} · {stamp(row.started_at)}
              </small>
              <strong>{label(row.phase)}</strong>
            </div>
            <Tag value={row.status} />
            <ChevronDown size={16} />
          </summary>
          <div className="decision-detail">
            <StatsLine
              items={[
                ["业务日期", dateText(row.run_date)],
                ["开始", stamp(row.started_at)],
                ["结束", stamp(row.finished_at)],
              ]}
            />
            {row.error && <p className="alert">{row.error}</p>}
            {row.stats?.steps?.length > 0 && (
              <Table
                headers={["执行步骤", "开始时间", "结束时间"]}
                rows={row.stats.steps.map((step) => [
                  label(step.stage),
                  stamp(step.started_at),
                  stamp(step.finished_at),
                ])}
              />
            )}
            <TextBlock title="任务结论">{row.stats?.summary}</TextBlock>
            <TextBlock title="本次模型状态">
              {row.stats?.model_status}
            </TextBlock>
            {row.stats?.report_id && (
              <a
                className="report-link"
                href={`?view=research&report=${row.stats.report_id}`}
              >
                查看本次报告 #{row.stats.report_id} ↗
              </a>
            )}
            {row.stats && Object.keys(row.stats).length > 0 && (
              <details className="evidence-fold">
                <summary>查看原始任务记录</summary>
                <pre>{JSON.stringify(row.stats, null, 2)}</pre>
              </details>
            )}
          </div>
        </details>
      ))}
    </div>
  ) : (
    <Empty>尚无调度任务记录。</Empty>
  );
}
export function Journal({ data, revision }) {
  const [tab, setTab] = useState("events");
  const status = data.status || {};
  return (
    <div className="page-stack">
      <StatsLine
        items={[
          [
            "调度器状态",
            status.scheduler_online == null
              ? "未知"
              : status.scheduler_online
                ? "在线"
                : "近期无心跳",
          ],
          ["最近心跳", stamp(status.last_heartbeat)],
          [
            "模型配置",
            status.llm_configured ? status.model || "已配置" : "未配置",
          ],
        ]}
      />
      <Tabs
        label="运行记录类型"
        value={tab}
        onChange={setTab}
        options={[
          ["events", "行为时间线"],
          ["runs", "调度任务"],
          ["schedule", "调度计划"],
        ]}
      />
      {status.watch_interval_minutes > 0 && (
        <p className="monitor-note">
          全天每 {status.watch_interval_minutes}{" "}
          分钟检查新增资讯和数据，出现新增证据后安排研究。
          {status.schedule?.intraday_decide &&
            `交易日 ${status.schedule.intraday_decide} 冻结模拟申赎指令。`}
        </p>
      )}
      {tab === "schedule" ? (
        <Panel
          title="自动运行计划"
          extra={
            <small className="quiet">
              {status.timezone || "Asia/Shanghai"}
            </small>
          }
        >
          {status.schedule && Object.keys(status.schedule).length ? (
            <div className="schedule">
              {Object.entries(status.schedule).map(([phase, entry]) => {
                const item =
                  typeof entry === "object" && entry !== null ? entry : null;
                return (
                  <div key={phase}>
                    <Clock3 size={16} />
                    <strong>{label(item?.phase || item?.name || phase)}</strong>
                    <span>
                      {item?.time ||
                        item?.cron ||
                        (item
                          ? Object.values(item).join(" · ")
                          : String(entry))}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty>服务未提供调度计划。</Empty>
          )}
        </Panel>
      ) : (
        <Panel
          title={tab === "events" ? "已记录的 Agent 行为" : "调度任务明细"}
        >
          <HistoryPage key={tab} kind={tab} revision={revision}>
            {(rows) =>
              tab === "events" ? (
                <Events items={rows} />
              ) : (
                <RunList rows={rows} />
              )
            }
          </HistoryPage>
        </Panel>
      )}
    </div>
  );
}
