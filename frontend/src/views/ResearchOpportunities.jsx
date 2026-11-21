import React, { useState } from "react";
import { ChevronDown } from "lucide-react";
import { dateText, Panel, Tag, TextBlock } from "../desk.jsx";
import { num, opportunityState, pct, pctAbs } from "../format.js";

function Memory({ memory }) {
  if (!memory?.samples) return <p>截至本次研究，尚无可引用的同板块同形态五日研究记录。</p>;
  return (
    <p>已到期且互不重叠的五日样本 {memory.samples} 个
      {memory.up_rate != null && `，五日后上涨占比 ${pctAbs(memory.up_rate, 1)}`}
      {memory.median_adverse_move != null && `，期间最不利变动中位数 ${pct(memory.median_adverse_move)}`}
      {memory.median_favorable_move != null && `，最有利变动中位数 ${pct(memory.median_favorable_move)}`}。
      {memory.up_rate == null && "满 8 个已验证样本才展示上涨频率与区间波动。"}历史上涨频率不能保证此次收益。</p>
  );
}

export default function ResearchOpportunities({ data }) {
  const [showAll, setShowAll] = useState(false);
  if (!data) return null;
  const signals = data.signals || [];
  const candidate = data.candidate;
  return (
    <Panel title="异动机会与试仓依据" extra={<small className="quiet">适用日 {dateText(data.application_date)}</small>}>
      <p className="panel-note">
        扫描 {data.boards_screened ?? "—"} 个板块，其中 {data.with_signal_data ?? "—"} 个有足够数据判断形态。
        {candidate ? `优先复核 ${[candidate.board, candidate.name, candidate.code].filter(Boolean).join(" · ")}。` :
          "本轮没有选出可提交复核的基金。"} 申赎金额及风控结论以当日决策指令为准。
      </p>
      {(candidate || data.rejections?.length > 0) && <div className="panel-inner">
        {candidate && <TextBlock title="基金净值与成本复核">
          {candidate.weight != null && `拟试探仓位 ${pctAbs(candidate.weight)}。`}{candidate.reason}
        </TextBlock>}
        {data.rejections?.length > 0 && <details className="evidence-fold">
          <summary>查看未通过基金复核的原因（{data.rejections.length} 条）</summary>
          <ul>{data.rejections.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
        </details>}
      </div>}
      {signals.length > 0 && <div className="watch-list">
        {(showAll ? signals : signals.slice(0, 8)).map((signal) => (
          <details className="decision" key={signal.board_id}>
            <summary>
              <div><small>行情截至 {dateText(signal.signal_date)}</small><strong>{signal.board} · {opportunityState(signal.state)}</strong></div>
              <Tag tone={signal.eligible ? "good" : "warm"}>{signal.eligible ? (signal.funds?.length ? "待基金复核" : "缺少基金映射") : "观察"}</Tag>
              <ChevronDown size={16} />
            </summary>
            <div className="decision-detail">
              <TextBlock title="当时的依据">{signal.evidence?.length
                ? <ul>{signal.evidence.map((line, i) => <li key={i}>{line}</li>)}</ul>
                : "该记录未附观察证据。"}</TextBlock>
              <TextBlock title="支撑与失效条件">
                {signal.support != null && `板块支撑参考 ${num(signal.support)} 点。`}
                {signal.invalidation || "该记录未附失效条件。"}
              </TextBlock>
              <TextBlock title="基金与仓位">
                {signal.funds?.length ? signal.funds.map((f, i) => <React.Fragment key={f.code}>
                  {i > 0 && "、"}{f.code} {f.name}
                  {/^https?:\/\//i.test(f.disclosure || "") && <> <a href={f.disclosure} target="_blank" rel="noreferrer">基金公告 ↗</a></>}
                </React.Fragment>) : "缺少直接映射的可用场外基金。"}
                {signal.eligible && signal.probe_weight != null && ` 形态允许的单笔上限 ${pctAbs(signal.probe_weight, 1)}。`}
              </TextBlock>
              <TextBlock title="历史日线中重现的形态">
                {signal.metrics?.analog_count == null ? "该形态未进行历史日线配对。" :
                  `独立五日样本 ${signal.metrics.analog_count} 个，随后上涨占比 ${pctAbs(signal.metrics.analog_hit_rate, 1)}，平均收益 ${pct(signal.metrics.analog_mean_5d)}。`}
              </TextBlock>
              <TextBlock title="经理此前记录并验证的形态"><Memory memory={signal.memory} /></TextBlock>
            </div>
          </details>
        ))}
      </div>}
      {signals.length > 8 && <div className="panel-inner"><button className="control" aria-expanded={showAll} onClick={() => setShowAll(!showAll)}>
        {showAll ? "收起至前 8 个板块" : `查看全部 ${signals.length} 个已记录形态的板块`}
      </button></div>}
    </Panel>
  );
}
