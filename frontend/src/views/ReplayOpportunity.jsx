import React, { useState } from "react";
import { Table } from "../components.jsx";
import { dateText, Empty, label, Panel, stamp, StateNotice, yuan } from "../desk.jsx";
import { num, opportunityAction, opportunityState, pct, pctAbs } from "../format.js";
import useResource from "../useResource.js";

function trialVariants(data) {
  if (!data) return [];
  const prior = data.previous_independent;
  const shared = data.previous_reserve;
  const variants = [];
  if (shared) variants.push({ key: "shared_reserve", name: "共享预留", data: shared });
  if (prior) variants.push({ key: "previous_independent", name: "原独立预留", data: prior });
  variants.push({ key: "opportunity", name: prior ? "去重修复版" : shared ? "独立预留" : "异动试仓", data });
  return variants;
}

function fundActivity(days, row, code) {
  const ledger = days.find((d) => d.date === row.date)?.opportunity;
  if (!Array.isArray(ledger?.orders)) return "缺少结算记录";
  const orders = ledger.orders.filter((o) => o.code === code);
  if (orders.length) return orders.map((o) => `${label(o.side)} ${yuan(o.amount)} 元`).join("、");
  if (ledger.plan?.code === code && ledger.plan.action === "hold") return "继续持有，未新增申赎";
  return "当日无该基金申赎";
}

function JuneAudit({ audit, correctedDays }) {
  if (!audit) return null;
  const checks = audit.unfilled_semiconductor;
  return (
    <Panel title="旧账户为什么错过六月科技行情">
      <p className="panel-note">{audit.summary}</p>
      <Table headers={["复盘日期", "旧账户", "实际记录", "当时的决策与约束"]}
        rows={audit.records.map((row) => [
          <a className="replay-link" href={`?view=replay&replay_tab=daily&version=${row.edition}&day=${row.day}#replay-days`}>{row.label}</a>,
          row.edition === "v1" ? "原版" : "优化版", row.activity, row.detail,
        ])} />
      <div className="panel-inner">
        <details className="evidence-fold">
          <summary>核对板块、基金涨幅与当时的数据覆盖</summary>
          <Table headers={["核验标的", "口径", "5 月 29 日", "6 月 30 日", "区间涨跌", "回放可见性"]} numeric={[2, 3, 4]}
            rows={audit.returns.map((row) => [
              row.name, row.kind, num(row.start, 4), num(row.end, 4), pct(row.end / row.start - 1), row.coverage,
            ])} />
          <p>{audit.coverage}</p>
          {audit.links.map((link, index) => <React.Fragment key={link.href}>
            {index > 0 && " · "}<a href={link.href} className="replay-link" target="_blank" rel="noreferrer">{link.label} ↗</a>
          </React.Fragment>)}
        </details>
        {checks?.rows?.length > 0 && <details className="evidence-fold">
          <summary>半导体异动 · {checks.rows.length} 次基金净值事后核验</summary>
          <p>{checks.instrument}。核心对照、共享预留、原独立预留三个账户在这四天均未申购该基金。以下涨跌以信号日收盘后公布的实际单位净值为起点。账户收益及试仓胜率单独按各版实际成交计算。</p>
          <Table headers={["信号日 T", "T 净值（元）", "T+1 交易日", "T+1 净值（元）", "1 日涨跌", "T+5 交易日", "T+5 净值（元）", "5 日涨跌", ...(correctedDays ? ["去重修复版实际操作"] : [])]} numeric={[1, 3, 4, 6, 7]}
            rows={checks.rows.map((r) => [r.date, num(r.nav, 4), r.next_date, num(r.next_nav, 4), pct(r.next_nav / r.nav - 1),
              r.fifth_date, num(r.fifth_nav, 4), pct(r.fifth_nav / r.nav - 1), ...(correctedDays ? [fundActivity(correctedDays, r, checks.code)] : [])])} />
          <a href={checks.url} className="replay-link" target="_blank" rel="noreferrer">{checks.source} ↗</a>
        </details>}
      </div>
    </Panel>
  );
}

function Comparisons({ data, variants }) {
  const segments = data.segments || [];
  function accounts(s) {
    return [["核心策略对照", s.baseline], ...variants.map((v) => [v.name, v.data.segments.find((row) => row.key === s.key)?.opportunity])];
  }
  return (
    <Panel title="独立账户对照" extra={<small className="quiet">{data.period?.trade_days ?? "—"} 个交易日</small>}>
      <p className="panel-note">{data.method}</p>
      {data.previous_reserve && <p className="panel-note">首轮试仓与核心交易共享月度换手额度。第二轮独立核算试仓月度申购额度，核心仓位的风控赎回保留执行权。
        {data.previous_independent && "第三轮沿用第二轮的资金限额和筛选条件，已有持仓及可实际执行的核心买入才占用同板块配置名额。"}</p>}
      <Table headers={["区间", "日期", "核心策略对照", ...variants.map((v) => v.name), "沪深300"]} numeric={[2, 3, 4, 5, 6]}
        rows={segments.map((s) => [
          s.label, `${s.start} 至 ${s.end}`, ...accounts(s).map(([, row]) => pct(row?.return)), pct(s.index_return),
        ])} />
      <Table headers={["区间", "账户 / 基准", "较对照收益差（百分点）", "最大回撤", "申赎费（元）", "申购笔数", "赎回笔数", "持仓天数"]} numeric={[2, 3, 4, 5, 6, 7]}
        rows={segments.flatMap((s) => [...accounts(s), ["沪深300", {return: s.index_return, max_drawdown: s.index_max_drawdown}]].map(([name, row]) => {
          const difference = row?.return != null && s.baseline?.return != null && row !== s.baseline
            ? pct(row.return - s.baseline.return).replace("%", "") : "—";
          return [s.label, name, difference, pctAbs(row?.max_drawdown), yuan(row?.fees), row?.buy_count ?? "—", row?.sell_count ?? "—", row?.exposure_days ?? "—"];
        }))} />
      <div className="panel-inner"><details className="evidence-fold">
        <summary>回放口径与局限</summary>
        <ul>{(data.limitations || []).map((line, i) => <li key={i}>{line}</li>)}</ul>
      </details></div>
    </Panel>
  );
}

function TrialTrades({ data }) {
  const trades = data.opportunity_trades || [];
  const checks = data.forward_checks || [];
  const exits = trades.filter((t) => t.side === "sell" && t.realized_net_pnl != null);
  return <>
    <Panel title="试仓后 1 日与 5 日的基金涨跌">
      <p className="panel-note">对每次试仓申购，用成交日净值与随后第 1、5 个交易日净值比较。分母为已到期且有净值的申购笔数，涨跌统计未扣申赎费。</p>
      <Table headers={["观察期", "申购样本", "已核验", "上涨笔数", "上涨占比", "平均净值涨跌", "最差一笔"]} numeric={[1, 2, 3, 4, 5, 6]}
        rows={checks.map((r) => [`${r.horizon_days} 个交易日`, r.total, r.scored, r.hits, pctAbs(r.hit_rate), pct(r.average_return), pct(r.worst_return)])} />
    </Panel>
    <Panel title="试仓成交明细" extra={<small className="quiet">{trades.length} 笔</small>}>
      {exits.length > 0 && <p className="panel-note">已结算的 {exits.length} 笔试仓赎回中，{exits.filter((t) => t.realized_net_pnl > 0).length} 笔扣费后盈利。每一笔按实际买卖净值及持有时间核算盈亏。</p>}
      {trades.length ? <Table headers={["日期", "操作", "基金", "入场形态", "成交金额（元）", "费用（元）", "已实现净损益（元）", "成交依据"]} numeric={[4, 5, 6]}
        rows={trades.map((t) => [
          dateText(t.date), label(t.side), `${t.name} · ${t.code}`, [t.board, opportunityState(t.state)].filter(Boolean).join(" · "),
          yuan(t.amount), yuan(t.fee), yuan(t.realized_net_pnl), t.reason,
        ])} /> : <Empty>本区间没有试仓成交，可按日查看观察和执行理由。</Empty>}
    </Panel>
  </>;
}

function JuneChanges({ days, audit, variants }) {
  function describe(ledger) {
    const fills = ledger?.orders?.filter((o) => o.is_probe) || [];
    if (fills.length) return fills.map((o) => `${label(o.side)} ${o.name}（${o.code}）${yuan(o.amount)} 元，费用 ${yuan(o.fee)} 元。${o.reason}`).join(" ");
    const plan = ledger?.plan;
    if (plan?.action === "enter" || plan?.action === "exit") return `${opportunityAction(plan.action)} ${plan.name}（${plan.code}），未成交。${(ledger.execution_notes || []).join(" ")}`;
    return [opportunityAction(plan?.action), plan?.name].filter(Boolean).join(" · ");
  }
  const changed = days.filter((d) => {
    if (!d.date.startsWith(audit.period)) return false;
    const decisions = variants.map(({ key }) => {
      const ledger = d[key];
      const fills = (ledger?.orders || []).filter((o) => o.is_probe).map((o) => [o.code, o.side]);
      return JSON.stringify([ledger?.plan?.action, ledger?.plan?.code, fills]);
    });
    return new Set(decisions).size > 1;
  });
  if (!changed.length) return null;
  return <Panel title="六月各版试仓出现分歧的日期">
    {audit.trial_review && <p className="panel-note">{audit.trial_review}</p>}
    <Table headers={["日期", "满足量价条件的板块", ...variants.map((v) => v.name)]} rows={changed.map((d) => [
      d.date, d.signals?.map((s) => s.board).join("、") || "当日无", ...variants.map((v) => describe(d[v.key])),
    ])} />
  </Panel>;
}

function DailyReview({ days, accountKey }) {
  const [date, setDate] = useState("");
  const day = days.find((item) => item.date === date) || days[0];
  if (!day) return null;
  const account = day[accountKey];
  const plan = account?.plan || {};
  const unfilled = days.filter((record) => {
    const ledger = record[accountKey];
    const intent = ledger?.plan;
    const side = {enter: "buy", exit: "sell"}[intent?.action];
    return side && Array.isArray(ledger.orders) && !ledger.orders.some(
      (order) => order.is_probe && order.code === intent.code && order.side === side,
    );
  });
  return <Panel title="每日判断与交易">
    {unfilled.length > 0 && <Table headers={["有试仓计划但未成交", "基金", "操作", "执行时的约束"]} rows={unfilled.map((d) => {
      const ledger = d[accountKey];
      return [<button className="replay-link" onClick={() => setDate(d.date)}>{d.date}</button>,
        [ledger.plan.board, ledger.plan.name, ledger.plan.code].filter(Boolean).join(" · "),
        opportunityAction(ledger.plan.action), ledger.execution_notes?.join(" ") || "未附执行说明。"];
    })} />}
    <div className="attr-controls"><label>交易日
      <select value={day.date} onChange={(event) => setDate(event.target.value)}>
        {days.map((item) => <option key={item.date} value={item.date}>{item.date}</option>)}
      </select>
    </label><small>资讯截至 {stamp(day.information_cutoff)} · 行情截至 {dateText(day.market_through)}</small></div>
    <div className="panel-inner">
      <p>达到形态条件的板块　{day.signals?.length
        ? day.signals.map((s) => `${s.board}（${opportunityState(s.state)}）`).join("、")
        : Array.isArray(day.signals) ? "当日无" : "未记录"}</p>
      <p>试仓判断　{[opportunityAction(plan.action), plan.name, plan.code].filter(Boolean).join(" · ")}。{plan.reason}</p>
      {account?.execution_notes?.map((line, i) => <p key={i}>{line}</p>)}
      {plan.rejections?.length > 0 && <details className="evidence-fold">
        <summary>其他机会未被采用的原因</summary>
        <ul>{plan.rejections.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
      </details>}
      {day.signals?.some((s) => s.evidence?.length) && <details className="evidence-fold">
        <summary>当日信号的量价、资金覆盖与失效条件</summary>
        {day.signals.map((s) => <div key={s.board_id}><strong>{s.board}</strong>
          <ul>{(s.evidence || []).map((v, i) => <li key={i}>{v}</li>)}</ul><p>{s.invalidation}</p>
        </div>)}
      </details>}
    </div>
    {account?.orders?.length ? <Table headers={["试仓账户当日全部成交", "基金", "操作", "金额（元）", "费用（元）", "依据"]} numeric={[3, 4]}
      rows={account.orders.map((o) => [o.is_probe ? "异动试仓" : "核心仓位", `${o.name} · ${o.code}`, label(o.side), yuan(o.amount), yuan(o.fee), o.reason])} /> :
      <Empty>{Array.isArray(account?.orders) ? "试仓账户当日没有成交。" : "缺少当日结算记录。"}</Empty>}
  </Panel>;
}

export default function ReplayOpportunity({ revision }) {
  const resource = useResource("/api/agent/replay/opportunity", revision);
  const data = resource.data;
  const [selected, setSelected] = useState("opportunity");
  const variants = trialVariants(data);
  const variant = variants.find((v) => v.key === selected) || variants.at(-1);
  return <div className="page-stack">
    <StateNotice resource={resource} />
    {data && <>
      {data.status === "completed" ? <>
        <Comparisons data={data} variants={variants} />
        <JuneAudit audit={data.june_audit} correctedDays={data.previous_independent && data.days} />
        {variants.length > 1 && data.june_audit && <JuneChanges days={data.days} audit={data.june_audit} variants={variants} />}
        {variants.length > 1 && <div className="desk-tabs" aria-label="试仓明细方案">
          {variants.slice().reverse().map((v) =>
            <button key={v.key} aria-pressed={variant.key === v.key} className={variant.key === v.key ? "selected" : ""} onClick={() => setSelected(v.key)}>{v.name}</button>
          )}
        </div>}
        <TrialTrades data={variant.data} />
        <DailyReview days={data.days || []} accountKey={variant.key} />
      </> : <>
        <Panel title="独立账户对照"><Empty>独立账户的异动试仓实验尚未归档。</Empty></Panel>
        <JuneAudit audit={data.june_audit} />
      </>}
    </>}
  </div>;
}
