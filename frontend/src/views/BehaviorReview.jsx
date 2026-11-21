import React, { useState } from "react";
import { ChevronDown } from "lucide-react";
import { Panel, dateText, sourceName, stamp } from "../desk.jsx";
import { num, pct, pctAbs } from "../format.js";

const regimeNames = {
  demand: "买方承接占优", distribution: "上涨后放量回落，兑现压力待验证", supply: "卖压占优",
  rotation: "资金方向切换，调仓换手待验证", mixed: "价量与资金尚无一致方向", insufficient: "资料不足",
};

function Feedback({ forecasts, row, published }) {
  return (
    <div className="bh-feedback">
      {forecasts.map((f) => {
        const memory = f.behavior_feedback;
        const change = f.behavior_adjustment ?? memory?.probability_shift;
        const issued = f.issued_at || published.find((item) =>
          item.board === row.board && item.horizon_days === f.horizon_days &&
          item.base_date === f.base_date && item.proxy_code === row.proxy_code
        )?.issued_at;
        return (
          <div key={f.horizon_days}>
            <strong>{f.horizon_days} 日上涨概率 · {pctAbs(f.prob_up)}</strong>
            <span>
              {f.behavior_locked && `沿用${issued ? " " + stamp(issued) : ""} 首次发布的判断。${f.behavior_regime
                ? "首发行为假设为“" + (regimeNames[f.behavior_regime] || f.behavior_regime) + "”。"
                : "首次发布早于行为研判。"}`}
              {f.base_prob_up != null && change != null
                ? `基础概率 ${pctAbs(f.base_prob_up)} · 行为校准 ${change > 0 ? "+" : ""}${num(change * 100, 2)} 个百分点`
                : "该次发布未记录行为校准。"}
            </span>
            {memory?.samples != null && (
              <span>
                同类已到期样本 {memory.samples} 条
                {memory.up_rate != null && ` · 实际上涨 ${pctAbs(memory.up_rate, 1)}`}
                {memory.samples < memory.min_samples && ` · 满 ${memory.min_samples} 条后参与校准`}
                {memory.mean_return != null && ` · 平均收益 ${pct(memory.mean_return)}`}
                {memory.brier_base != null && ` · 基础概率 Brier 分数 ${num(memory.brier_base, 3)}`}
                {memory.first_target && memory.last_target && ` · 到期区间 ${dateText(memory.first_target)} 至 ${dateText(memory.last_target)}`}
              </span>
            )}
            {memory?.brier_adjusted != null && (
              <span>配对样本 {memory.paired_samples} 条 · Brier 分数 {num(memory.brier_base_paired, 3)} → {num(memory.brier_adjusted, 3)}
                {memory.brier_gain != null && ` · 误差${memory.brier_gain > 0 ? "降低" : memory.brier_gain < 0 ? "增加" : "持平"}${memory.brier_gain ? " " + num(Math.abs(memory.brier_gain), 3) : ""}`}</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ObservationList({ rows }) {
  return (
    <div className="bh-observations">
      {rows.map((o, index) => (
        <div key={o.key || index}>
          <small>{o.label}</small>
          <strong>{o.value == null ? "—" : `${typeof o.value === "number" ? num(o.value, 2) : o.value}${o.unit || ""}`}</strong>
          <small>{o.source && sourceName(o.source)}{o.at && ` · ${String(o.at).includes("T") ? stamp(o.at) : dateText(o.at)}`}</small>
        </div>
      ))}
    </div>
  );
}

function Notes({ label, values }) {
  if (!values?.length) return null;
  return (
    <div className="bh-notes">
      <b>{label}</b>
      <ul>{values.map((line, i) => <li key={i}>{line}</li>)}</ul>
    </div>
  );
}

function PriorPlan({ plan }) {
  if (!plan) return null;
  const stages = {pending_close: "等待收盘", partial: "部分资料可核验", observed: "已取得后续观测", missing_data: "缺少核验数据"};
  return (
    <details className="bh-fold">
      <summary>上一份计划的跟踪 <ChevronDown size={15}/></summary>
      <div className="bh-prior">
        <strong>{plan.label} · T+{plan.day} · {dateText(plan.checkpoint_date)}</strong>
        <p>{plan.watch}</p>
        {plan.confirm && <p>支持条件　{plan.confirm}</p>}
        {plan.invalidate && <p>失效条件　{plan.invalidate}</p>}
        <p>{stages[plan.status] || plan.status}{plan.note && `　·　${plan.note}`}</p>
        {plan.observed_close_date && <p>收盘观测　{dateText(plan.anchor_date)} → {dateText(plan.observed_close_date)}
          {plan.realized_board_return != null && `　·　板块收益 ${pct(plan.realized_board_return)}`}
          {plan.close_source && `　·　${sourceName(plan.close_source)}`}</p>}
        {plan.net_yuan != null && <p>当日净流入　{num(plan.net_yuan / 1e8)} 亿元 · {dateText(plan.flow_date)}
          {plan.flow_source && `　·　${sourceName(plan.flow_source)}`}</p>}
        {plan.report_id && <a className="report-link" href={`?view=research&report=${plan.report_id}`}>原研究报告 #{plan.report_id} ↗</a>}
      </div>
    </details>
  );
}

function BehaviorDetails({ row, published }) {
  const b = row.behavior;
  const impact = b.impact;
  const forecasts = (row.outlooks || []).filter(
    (f) => f.behavior_feedback || f.base_prob_up != null || f.behavior_locked,
  );
  const factor = impact?.risk_multiplier ?? b.risk_multiplier;
  return (
    <div className="bh-body">
      <div className="bh-conclusion">
        <div><small>当前行为假设</small><h3>{b.label}</h3></div>
        {factor != null && <div className="bh-budget"><small>风险预算系数</small><strong>{num(factor, 2)} ×</strong></div>}
      </div>
      {b.risk_reason && <p className="bh-reason">{b.risk_reason}</p>}
      {impact && (
        <div className="bh-impact">
          {row.intent !== "observe" && impact.before_weight != null && impact.after_weight != null && (
            <strong>映射基金目标仓位 {pctAbs(impact.before_weight, 1)} → {pctAbs(impact.after_weight, 1)}</strong>
          )}
          {impact.note && <span>{impact.note}</span>}
        </div>
      )}
      {!!b.hypotheses?.length && (
        <section className="bh-section">
          <h4>可能的交易动机</h4>
          <div className="bh-hypotheses">
            {b.hypotheses.map((h) => (
              <article key={h.key || h.title}>
                <h5>{h.title}</h5>
                <p>{h.interpretation}</p>
                <Notes label="支持线索" values={h.support} />
                <Notes label="相反线索" values={h.against} />
                {h.confirm && <p><b>后续验证</b>　{h.confirm}</p>}
                {h.invalidate && <p><b>推翻条件</b>　{h.invalidate}</p>}
              </article>
            ))}
          </div>
        </section>
      )}
      {!!b.path?.length && (
        <details className="bh-fold">
          <summary>未来 {b.path.length} 个交易日的观察计划 <ChevronDown size={15}/></summary>
          <ol className="bh-path">
            {b.path.map((step) => (
              <li key={step.day}>
                <div className="bh-date"><b>T+{step.day}</b>{step.target_date && <small>{dateText(step.target_date)}</small>}</div>
                <div>
                  <strong>{step.watch}</strong>
                  {step.confirm && <p><b>支持条件</b>　{step.confirm}</p>}
                  {step.invalidate && <p><b>失效条件</b>　{step.invalidate}</p>}
                </div>
              </li>
            ))}
          </ol>
        </details>
      )}
      {!!b.observations?.length && (
        <details className="bh-fold">
          <summary>研判使用的 {b.observations.length} 项观测 <ChevronDown size={15}/></summary>
          <ObservationList rows={b.observations} />
        </details>
      )}
      <PriorPlan plan={row.prior_behavior}/>
      {forecasts.length > 0 && <details className="bh-fold"><summary>相似行为的到期反馈 <ChevronDown size={15}/></summary><Feedback forecasts={forecasts} row={row} published={published}/></details>}
      {b.limitations?.length > 0 && <p className="bh-limits">{b.limitations.join(" ")}</p>}
    </div>
  );
}

export default function BehaviorReview({ items = [], published = [] }) {
  const rows = items.filter((r) => r.behavior);
  const [picked, setPicked] = useState(null);
  if (!rows.length) return null;
  const selected = rows.find((r) => r.board_id === picked) || rows[0];
  return (
    <Panel title="资金行为研判">
      <div className="bh-toolbar" aria-label="选择研判板块">
        {rows.map((r) => <button type="button" key={r.board_id} aria-pressed={selected === r}
          onClick={() => setPicked(r.board_id)}>{r.board}</button>)}
      </div>
      <BehaviorDetails key={selected.board_id} row={selected} published={published} />
    </Panel>
  );
}
