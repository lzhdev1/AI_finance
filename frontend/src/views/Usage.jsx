import React, { useState } from "react";
import { ChevronDown, Cpu, Database, FileSearch, ReceiptText } from "lucide-react";
import { Table } from "../components.jsx";
import { Empty, label, Panel, stamp, StateNotice, Tag } from "../desk.jsx";
import useResource from "../useResource.js";
import "./usage.css";

const kinds = { all: "全部调用", llm: "模型", search: "搜索与检索", data: "数据采集", tool: "研究工具" };
const ranges = { 1: "今天", 7: "近 7 天", 30: "近 30 天", all: "全部时间" };
const integer = new Intl.NumberFormat("zh-CN");
const count = (value) => value == null ? "未提供" : integer.format(value);
const pageSize = 30;

function amount(value, currency) {
  if (value == null || !currency) return "—";
  const raw = String(value);
  const match = raw.match(/^(-?\d+)(?:\.(\d+))?$/);
  if (!match) return `${currency} ${raw}`;
  const whole = match[1].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = (match[2] || "").replace(/0+$/, "");
  return `${currency} ${whole}${fraction ? "." + fraction : ""}`;
}

function Metric({ title, children, note, icon: Icon }) {
  return <div className="metric">
    <div className="metric-title">{title}<Icon size={16} /></div>
    <div className="metric-number">{children}</div>
    <div className="metric-note">{note}</div>
  </div>;
}

function CostTotals({ costs, basis }) {
  const rows = costs.filter((row) => row[`${basis}_amount`] != null);
  return rows.length ? <span className="usage-amounts">
    {rows.map((row) => <span key={row.currency}>{amount(row[`${basis}_amount`], row.currency)}</span>)}
  </span> : "—";
}

function Charges({ summary, groups }) {
  return <Panel title="费用明细" extra={<small className="quiet">按原币统计</small>}>
    <div className="usage-charges">
      <div><small>服务商回报金额</small><strong><CostTotals costs={summary.costs} basis="reported" /></strong></div>
      <div><small>按费率估算</small><strong><CostTotals costs={summary.costs} basis="estimated" /></strong></div>
    </div>
    <Table headers={["服务及模型", "调用类型", "调用次数", "已记录 Token", "服务商回报", "费率估算", "待计价", "无按次费用"]}
      numeric={[2, 3, 4, 5, 6, 7]} rows={groups.map((g) => [
        <div className="cell-stack"><b>{g.provider}</b>{g.model && <small>{g.model}</small>}</div>,
        kinds[g.kind] || g.kind, count(g.calls),
        g.kind === "llm" ? <div className="cell-stack">{count(g.total_tokens)}
          {g.missing_token_calls > 0 && <small>{count(g.missing_token_calls)} 次缺报</small>}</div> : "—",
        <CostTotals costs={g.costs} basis="reported" />, <CostTotals costs={g.costs} basis="estimated" />,
        count(g.unknown_cost_calls), count(g.no_charge_calls),
      ])} />
    <p className="panel-note">本地资料检索无按次 API 费用。公开数据请求缺少服务商报价时记为待计价。费用估算采用调用时的价格，可展开逐次记录核对。</p>
  </Panel>;
}

function ReplayArchiveUsage({ archives }) {
  if (!archives?.length) return null;
  return <Panel title="历史回放用量">
    <div className="panel-inner">
      <details className="evidence-fold">
        <summary>查看 {archives.length} 个封存回放账户</summary>
        <Table headers={["回放账户", "有计量记录的报告", "已记录 Token", "服务商回报", "费率估算"]}
          numeric={[1, 2, 3, 4]} rows={archives.map((entry) => [
            ({ v1: "原版经理", v2: "优化版经理" })[entry.version] || entry.version,
            `${count(entry.tracked_reports)} / ${count(entry.total_reports)}`,
            count(entry.summary?.total_tokens),
            <CostTotals costs={entry.summary?.costs || []} basis="reported" />,
            <CostTotals costs={entry.summary?.costs || []} basis="estimated" />,
          ])} />
        <p className="panel-note">仅统计回放当时保存的记录。缺少用量的报告无法核算费用。</p>
      </details>
    </div>
  </Panel>;
}

const bases = { reported: "服务商回报", estimated: "费率估算", unknown: "待计价", no_charge: "无按次费用" };
function EventRow({ event }) {
  const modelCall = event.kind === "llm";
  const priced = ["estimated", "reported"].includes(event.cost_basis);
  return <details className="usage-event">
    <summary>
      <div className="usage-event-action">
        <small>{stamp(event.occurred_at)} · {kinds[event.kind] || event.kind}</small>
        <strong>{event.operation}</strong>
        <small>{[event.provider, event.model].filter(Boolean).join(" · ")}</small>
      </div>
      <div className="usage-event-tokens">{modelCall && <><b>{count(event.total_tokens)}</b><small>Token</small></>}</div>
      <div className="usage-event-cost"><b>{priced ? amount(event.amount, event.currency) : bases[event.cost_basis] || "待计价"}</b>
        {priced && <small>{bases[event.cost_basis]}</small>}</div>
      <Tag value={event.status}>{event.status === "success" ? "成功" : label(event.status)}</Tag>
      <ChevronDown size={15} />
    </summary>
    <div className="usage-event-detail">
      <div className="usage-origin">
        {event.report_id && <a className="report-link" href={`?view=research&report=${event.report_id}`}>报告 #{event.report_id} ↗</a>}
        {event.run_id && <span>任务 #{event.run_id}</span>}
        {event.scope && <span>{event.scope === "historical_replay" ? "历史回放" : label(event.scope)}</span>}
      </div>
      {modelCall && <dl className="usage-tokens">
        {[["输入 Token", event.input_tokens], ["输出 Token", event.output_tokens],
          ["缓存命中 Token", event.cached_tokens], ["推理 Token", event.reasoning_tokens]].map(([k, v]) =>
          <div key={k}><dt>{k}</dt><dd>{count(v)}</dd></div>)}
      </dl>}
      {event.detail && <p>{event.detail}</p>}
      {event.price_note && <p>{event.price_note}</p>}
      {/^https?:\/\//i.test(event.source_url || "") && <a className="report-link" href={event.source_url}
        target="_blank" rel="noreferrer">查看计价来源 ↗</a>}
    </div>
  </details>;
}

function readFilters() {
  const params = new URLSearchParams(window.location.search);
  const days = params.get("days");
  const kind = params.get("kind");
  return { days: Object.hasOwn(ranges, days) ? days : "30", kind: Object.hasOwn(kinds, kind) ? kind : "all",
    report: params.get("report") || "", offset: 0 };
}

export default function Usage({ revision, enabled }) {
  const [filters, setFilters] = useState(readFilters);
  const { days, kind, report, offset } = filters;
  function apply(change) {
    const next = { ...filters, offset: 0, ...change };
    setFilters(next);
    const url = new URL(window.location.href);
    for (const key of ["days", "kind", "report"]) {
      if (next[key]) url.searchParams.set(key, next[key]);
      else url.searchParams.delete(key);
    }
    window.history.replaceState({}, "", url);
  }
  const query = new URLSearchParams({ days: report ? "all" : days, kind, offset, limit: pageSize });
  if (report) query.set("report_id", report);
  const resource = useResource(enabled ? `/api/agent/usage?${query}` : null, revision);
  if (!enabled) return <Empty>此工作台未开放用量与费用。</Empty>;
  const data = resource.data;
  const s = data?.summary;
  const notTracked = report && data?.report_coverage === "not_tracked";
  const trackingFrom = data?.tracking_started_at ? new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(data.tracking_started_at)) : null;
  return <div className="page-stack usage-page">
    <div className="toolbar">
      {report ? <div className="usage-report">
        <a className="report-link" href={`?view=research&report=${encodeURIComponent(report)}`}>研究报告 #{report} ↗</a>
        <button className="control" onClick={() => apply({ report: "" })}>查看全部用量</button>
      </div> : <label>线上经理用量 <select value={days} onChange={(e) => apply({ days: e.target.value })}>
        {Object.entries(ranges).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
      </select></label>}
      <label>调用类型 <select value={kind} onChange={(e) => apply({ kind: e.target.value })}>
        {Object.entries(kinds).map(([id, text]) => <option key={id} value={id}>{text}</option>)}
      </select></label>
      {!report && (days !== "all" || kind !== "all") && <button className="more"
        onClick={() => apply({ days: "all", kind: "all" })}>查看历史回放用量</button>}
      {trackingFrom && <small className="usage-start">自 {trackingFrom}（上海）起采集</small>}
    </div>
    <StateNotice resource={resource} />
    {data && <ReplayArchiveUsage archives={data.replay_archives} />}
    {s && (notTracked ? <Panel title="报告用量">
      <Empty>这份报告没有关联用量记录，历史 Token 和费用无法核实。</Empty>
      {data.historical_tools?.length > 0 && <>
        <p className="panel-note">原报告保存了以下工具操作，可核对调用次数。</p>
        <Table headers={["已归档操作", "来源", "类型", "调用次数"]} numeric={[3]}
          rows={data.historical_tools.map((tool) => [tool.operation, tool.provider, kinds[tool.kind] || tool.kind, count(tool.count)])}/>
      </>}
    </Panel> :
      s.calls === 0 ? <Panel title={report ? "报告用量" : "调用记录"}><Empty>{report
        ? "本报告在所选调用类型下没有记录。" : "所选时间和调用类型内尚无记录。"}</Empty></Panel> :
      <>
        <div className="metrics usage-metrics">
          <Metric title="已记录模型 Token" icon={Cpu} note={s.llm_calls
            ? `${count(s.llm_calls)} 次模型调用 · ${count(s.missing_token_calls)} 次缺少总量` : "所选范围内没有模型调用"}>
            {s.llm_calls ? count(s.total_tokens) : "—"}
          </Metric>
          <Metric title="搜索与检索" icon={FileSearch} note="含已入库新闻检索">{count(s.search_calls)}<small>次</small></Metric>
          <Metric title="数据与工具" icon={Database} note={`数据采集 ${count(s.data_calls)} 次 · 研究工具 ${count(s.tool_calls)} 次`}>
            {count(s.data_calls + s.tool_calls)}<small>次</small>
          </Metric>
          <Metric title="待计价调用" icon={ReceiptText} note={`另有 ${count(s.no_charge_calls)} 次无按次费用`}>
            {count(s.unknown_cost_calls)}<small>次</small>
          </Metric>
        </div>
        {s.llm_calls > 0 && <section className="summary-line" aria-label="模型 Token 明细">
          {[["已记录输入 Token", s.input_tokens], ["已记录输出 Token", s.output_tokens], ["其中缓存命中", s.cached_tokens],
            ["其中推理", s.reasoning_tokens]].map(([name, val]) => <div key={name}><span>{name}</span><b>{count(val)}</b></div>)}
        </section>}
        <Charges summary={s} groups={data.groups} />
        <Panel title="逐次调用" extra={<small className="quiet">共 {count(s.calls)} 次 · 失败 {count(s.errors)} 次</small>}>
          {data.events.map((event) => <EventRow event={event} key={event.id} />)}
          {data.total > pageSize && <div className="pager"><span>第 {count(data.offset + 1)}–{count(data.offset + data.events.length)} 条，共 {count(data.total)} 条</span>
            <button onClick={() => apply({ offset: Math.max(0, offset - pageSize) })} disabled={offset === 0}>上一页</button>
            <button onClick={() => apply({ offset: offset + pageSize })} disabled={offset + pageSize >= data.total}>下一页</button>
          </div>}
        </Panel>
      </>)}
  </div>;
}
