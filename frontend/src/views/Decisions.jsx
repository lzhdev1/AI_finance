import React, { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import { HistoryPage } from "../Archive.jsx";
import { Table } from "../components.jsx";
import { dateText, Empty, label, Panel, sourceName, stamp, StateNotice, Tag, TextBlock, yuan } from "../desk.jsx";
import { forecastDirection, opportunityAction, pct, pctAbs } from "../format.js";
import useResource from "../useResource.js";

const shanghaiClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const clock = (iso) => shanghaiClock.format(new Date(iso));

export function useCurrentDecision(enabled, revision) {
  const [poll, setPoll] = useState(0);
  const result = useResource(enabled ? "/api/agent/decisions/today" : null, `${revision}-${poll}`, 15000);
  const day = result.data;
  const time = day?.as_of ? clock(day.as_of).slice(0, 5) : null;
  const fast = day?.is_trade_day && !day.decision && time >= day.schedule?.prepare && time <= day.schedule?.cutoff;
  useEffect(() => {
    if (!enabled) return;
    const refresh = () => { if (!document.hidden) setPoll((n) => n + 1); };
    const timer = result.loading ? null : window.setTimeout(refresh, fast || result.error ? 5000 : 30000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [enabled, result.loading, result.error, fast, poll]);
  return result;
}

const states = {
  scheduled: "等待盘中研究",
  preparing: "盘中研究时段",
  reviewing: "等待最终指令",
  overdue: "已超过目标发布时间",
  missed: "今日截止前未发布指令",
  closed: "今日休市",
  calendar_unknown: "交易日历待确认",
};
const methods = {
  final_review: "最终研究复核",
  prepared_review: "采用已完成的盘中研究",
  protective_hold: "保护性维持",
};
function actionName(row) {
  const action = row.payload?.planned_action || row.action;
  if (action === "cash") return "继续空仓";
  if (action === "hold") return "维持持仓";
  return action === "pending" ? "已冻结申赎指令" : label(action);
}
function Settlement({ row }) {
  const status = row.payload?.settlement?.status || (row.executed_at ? "settled" : "pending");
  return <Tag value={status}>{status === "pending" ? "待结算" : label(status)}</Tag>;
}
function FundEstimates({ orders, estimates }) {
  const funds = new Map(orders.map((order) => [order.code, order.name || order.code]));
  function estimateCell(code, horizon) {
    const item = estimates.find((f) => f.code === code && Number(f.horizon_days) === horizon);
    if (item?.prob_up == null) return "未收录估计";
    return (
      <div className="cell-stack">
        <b>{forecastDirection(item.prob_up)} · 上涨概率 {pctAbs(item.prob_up, 1)}</b>
        <small>{dateText(item.target_date)}{item.expected_return != null && ` · 预期收益 ${pct(item.expected_return)}`}</small>
        {item.status === "estimate" && <small>预案试算</small>}
      </div>
    );
  }
  return (
    <TextBlock title="下单基金的净值判断">
      {estimates.length ? (
        <Table headers={["基金", "未来 1 个交易日", "未来 5 个交易日"]}
          rows={[...funds].map(([code, name]) => [
            <div className="cell-stack"><b>{name}</b>{name !== code && <small>{code}</small>}</div>,
            estimateCell(code, 1), estimateCell(code, 5),
          ])}
        />
      ) : "本条指令未收录下单基金的 1 日、5 日净值估计。具体申赎理由见决策依据与风控检查。"}
    </TextBlock>
  );
}
function DecisionDetails({ row }) {
  const p = row.payload || {};
  const pub = p.publication;
  const orders = p.instructions || p.pending || [];
  const probe = p.opportunity;
  const probeOrder = probe && orders.find((order) => order.code === probe.code &&
    ((probe.action === "enter" && order.side === "buy" && order.opportunity_probe) ||
      (probe.action === "exit" && order.side === "sell")));
  const horizons = pub?.forecast_targets;
  const snapshot = pub?.inputs?.snapshot;
  const news = pub?.inputs?.news;
  const responses = Object.entries(news || {}).filter(([, result]) => Number.isInteger(result?.rows));
  return (
    <div className="decision-detail">
      {orders.length ? (
        <Table headers={["模拟操作", "基金", "申购金额（元）", "赎回份额（份）"]} numeric={[2, 3]}
          rows={orders.map((order) => [
            <Tag value={order.side}>{label(order.side)}</Tag>,
            <div className="cell-stack"><b>{order.name || order.code}</b>{order.name && <small>{order.code}</small>}
              {order.opportunity_probe && <small>异动试仓</small>}</div>,
            order.side === "buy" ? yuan(order.amount) : "—",
            order.side === "sell" ? yuan(order.shares, 6) : "—",
          ])}
        />
      ) : <p className="decision-no-orders">当日无模拟申购或赎回。</p>}
      <dl className="decision-times">
        {(pub?.published_at || row.created_at) && <div><dt>{pub?.published_at ? "发布时间" : "记录时间"}</dt><dd>{clock(pub?.published_at || row.created_at)}</dd></div>}
        {pub?.evidence_as_of && <div><dt>研究资料截至</dt><dd>{stamp(pub.evidence_as_of)}:{clock(pub.evidence_as_of).slice(-2)}</dd></div>}
        {pub?.reviewed_through && <div><dt>发布前核查至</dt><dd>{stamp(pub.reviewed_through)}:{clock(pub.reviewed_through).slice(-2)}</dd></div>}
      </dl>
      {pub?.source && (
        <TextBlock title="最终复核">
          <strong>{methods[pub.source] || pub.source}</strong>
          {pub.reason && <p>{pub.reason}</p>}
        </TextBlock>
      )}
      <TextBlock title="决策依据">{row.rationale}</TextBlock>
      {probe && <TextBlock title="异动试仓复核">
        <strong>{[opportunityAction(probe.action), probe.board, probe.code].filter(Boolean).join(" · ")}</strong>
        <p>{probe.reason}</p>
        {probe.rejections?.length > 0 && <details className="evidence-fold">
          <summary>其他已评估的机会与未采用原因</summary>
          <ul>{probe.rejections.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
        </details>}
        {["enter", "exit"].includes(probe.action) && <p>{probeOrder
          ? "已生成相应的模拟申赎指令，成交以净值结算记录为准。"
          : "本次未生成对应的试仓申赎指令，执行约束见本条风控检查。"}</p>}
      </TextBlock>}
      {(snapshot || responses.length > 0 || news?.warnings?.length > 0) && (
        <TextBlock title="本轮采集回执">
          <ul>
            {snapshot && <li>盘中快照 {stamp(snapshot.ts)}，覆盖 {snapshot.boards ?? "—"} 个板块、{snapshot.indexes ?? "—"} 个指数。</li>}
            {responses.map(([source, result]) => <li key={source}>{sourceName(source)}返回 {result.rows} 条新闻{result.inserted != null && `，新增入库 ${result.inserted} 条`}。</li>)}
            {news?.warnings?.map((warning, i) => <li key={i}>{warning}</li>)}
          </ul>
        </TextBlock>
      )}
      {pub && (
        <TextBlock title="决策对应的未来行情">
          <div className="decision-horizons">
            <span>下一交易日收盘 <b>{horizons?.["1"] ? dateText(horizons["1"]) : "目标日期待确认"}</b></span>
            <span>第 5 个交易日收盘 <b>{horizons?.["5"] ? dateText(horizons["5"]) : "目标日期待确认"}</b></span>
          </div>
          <p>以 {dateText(row.trade_date)} 收盘为基准，考察次一交易日涨跌和未来 5 个交易日累计涨跌。板块按报告指定的指数收盘值或代理基金净值验证，基金按自身单位净值验证。</p>
          {pub.source === "protective_hold" && <p>本轮保护性维持未新增行情预测。</p>}
        </TextBlock>
      )}
      {!!orders.length && pub && <FundEstimates orders={orders} estimates={pub.fund_outlooks || []} />}
      {p.research_id && <a className="report-link" href={`?view=research&report=${p.research_id}`}>查看研究依据与行情预测 · 报告 #{p.research_id} ↗</a>}
      {!!p.risk_checks?.length && <TextBlock title="风控检查"><ul>{p.risk_checks.map((note, i) => <li key={i}>{note}</li>)}</ul></TextBlock>}
      <TextBlock title="判断失效条件">{row.invalidation_note}</TextBlock>
      <TextBlock title="结算说明">{p.settlement?.reason}</TextBlock>
      {row.executed_at && <small className="quiet">结算确认 {stamp(row.executed_at)}</small>}
    </div>
  );
}

function Today({ day }) {
  const row = day.decision;
  const schedule = day.schedule || {};
  if (row && dateText(row.trade_date) === day.trade_date) {
    const publishedAt = row.payload?.publication?.published_at;
    const late = publishedAt && schedule.target && (row.payload.publication.late ?? (clock(publishedAt) > `${schedule.target}:00`));
    return (
      <article className="decision-today">
        <div className="decision-heading">
          <h3>{actionName(row)}</h3>
          <Settlement row={row} />
        </div>
        {late && <p>实际发布时间 {clock(publishedAt)}，晚于当日 {schedule.target} 目标。</p>}
        <DecisionDetails row={row} />
      </article>
    );
  }
  let note = "";
  if (["scheduled", "preparing", "reviewing"].includes(day.state)) {
    note = `计划于 ${schedule.review}–${schedule.target} 发布最终操作，模拟申赎截止 ${schedule.cutoff}。`;
  } else if (day.state === "overdue") {
    note = `截至 ${clock(day.as_of)} 尚无最终指令，今日模拟申赎截止 ${schedule.cutoff}。`;
  } else if (day.state === "missed") {
    note = "当日申赎窗口已结束。可在运行日志查看任务记录。";
  } else if (day.state === "closed" && day.next_trade_date) {
    note = `下一交易日为 ${dateText(day.next_trade_date)}。`;
  }
  return (
    <div className="decision-today" role="status">
      <div className="decision-heading"><h3>{states[day.state] || "暂未取得当日指令"}</h3></div>
      {note && <p>{note}</p>}
      {day.is_trade_day && day.scheduler_online === false && <p className="alert">近期没有收到服务器调度心跳。</p>}
      {["overdue", "missed"].includes(day.state) && <a className="report-link" href="?view=journal">查看运行日志 ↗</a>}
    </div>
  );
}

export default function DecisionHistory({ resource, revision }) {
  const day = resource.data;
  return (
    <div className="page-stack">
      <Panel title={resource.error && day ? "最近读取的最终决策" : "当日最终决策"} extra={day && <small className="quiet">{day.trade_date} · 上海时间</small>}>
        <StateNotice resource={resource} />
        {day && <Today day={day} />}
      </Panel>
      {(day || resource.error) && <Panel title="历史决策指令">
        <HistoryPage kind="decisions" revision={`${revision}-${day?.trade_date || ""}`}>
          {(rows) => {
            const history = rows.filter((r) => dateText(r.trade_date) !== day?.trade_date);
            return history.length ? (
              <div className="watch-list">
                {history.map((row) => (
                  <details className="decision" key={row.decision_id || row.trade_date}>
                    <summary>
                      <div><small>{dateText(row.trade_date)} · {actionName(row)}</small><strong>{row.summary || actionName(row)}</strong></div>
                      <Settlement row={row} /><ChevronDown size={16} />
                    </summary>
                    <DecisionDetails row={row} />
                  </details>
                ))}
              </div>
            ) : <Empty>暂无历史决策指令。</Empty>;
          }}
        </HistoryPage>
      </Panel>}
    </div>
  );
}
