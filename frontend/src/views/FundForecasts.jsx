import React from "react";
import { ChevronDown } from "lucide-react";
import { dateText, Delta, Empty, label, sourceName, stamp, Tag } from "../desk.jsx";
import { forecastDirection, num, pctAbs } from "../format.js";
import { SourceLink } from "./Market.jsx";

function NavField({ label: title, day, value, missing = "待取得" }) {
  return (
    <div>
      <small>{title}</small>
      <strong>{value == null ? missing : `${num(value, 4)} 元/份`}</strong>
      <span>{dateText(day)}</span>
    </div>
  );
}

function FundClock({ clock }) {
  if (!clock) return null;
  return (
    <section className="ff-clock">
      <h4>模拟申赎时钟</h4>
      <div className="ff-clock-dates">
        <div><small>可申请日 T</small><strong>{dateText(clock.apply_date)}</strong></div>
        <div><small>预计确认日</small><strong>{dateText(clock.confirm_date)}</strong></div>
        <div><small>最早可赎回日</small><strong>{dateText(clock.earliest_redeem_date)}</strong></div>
        {clock.redemptions_available_date && <div><small>若 T 日赎回，款项可再申购日</small><strong>{dateText(clock.redemptions_available_date)}</strong></div>}
      </div>
      {clock.decision_window && <p>决策窗口　{clock.decision_window}</p>}
      {clock.pricing && <p>{clock.pricing}</p>}
      {clock.payout && <p>{clock.payout}</p>}
      {clock.rule_source && <p><SourceLink source={clock.rule_source} url={clock.source_url} /></p>}
    </section>
  );
}

function FundFees({ estimate }) {
  if (estimate?.application_date == null || estimate.evaluation_date == null) return null;
  return (
    <section className="ff-clock">
      <h4>本次研究的申赎成本估算</h4>
      <p>若在 {dateText(estimate.application_date)} 申购、{dateText(estimate.evaluation_date)} 赎回，预计持有 {estimate.holding_days} 个自然日。</p>
      <div className="ff-numbers">
        <div><small>适用赎回费率</small><strong>{pctAbs(estimate.redemption_rate, 2)}</strong></div>
        <div><small>覆盖往返费用所需涨幅</small><strong>{pctAbs(estimate.round_trip_fee, 4)}</strong></div>
      </div>
    </section>
  );
}

export default function FundForecasts({ rows = [], features, reportView = false }) {
  if (!rows.length) return <Empty>当前记录中没有具体基金的净值预测。</Empty>;
  return (
    <div className="watch-list ff-list">
      {rows.map((r) => {
        const scored = r.status === "scored";
        const insufficient = r.status === "insufficient";
        const result = scored
          ? (r.hit == null ? "已核验 · 不计方向" : r.hit ? "方向命中" : "方向未命中")
          : insufficient ? "未形成预测" : "待净值核验";
        const horizon = r.horizon_days === 1 ? "次日" : `${r.horizon_days} 日`;
        return (
          <details className="decision ff-row" key={r.id ?? `${r.report_id}-${r.code}-${r.horizon_days}-${r.base_date}`}>
            <summary>
              <div className="ff-subject"><small>{r.code} · {horizon}单位净值</small><strong>{r.name}</strong></div>
              <div className="ff-prediction"><strong>{{ up: "看涨", down: "看跌", neutral: "中性" }[r.direction] || forecastDirection(r.prob_up)}</strong><small>上涨概率 {pctAbs(r.prob_up)}</small></div>
              <div className="ff-target"><strong>{dateText(r.target_date)}</strong><small>目标净值日</small></div>
              <Tag tone={scored ? (r.hit === true ? "good" : r.hit === false ? "bad" : "") : "warm"}>{result}</Tag>
              <ChevronDown size={16}/>
            </summary>
            <div className="decision-detail">
              <p className="ff-period">
                {dateText(r.base_date)} → {dateText(r.target_date)}　·　发布于 {stamp(r.issued_at)}
                {!reportView && r.report_id && <a className="report-link" href={`?view=research&report=${r.report_id}`}>研究报告 #{r.report_id} ↗</a>}
              </p>
              {r.selected_by && <p className="ff-period">{reportView ? "本次研究的跟踪原因" : insufficient ? "记录时的跟踪原因" : "首次预测的跟踪原因"}　{r.selected_by}</p>}
              {r.reason && <p className={insufficient ? "data-note" : "ff-period"}>{r.reason}</p>}
              {!insufficient && (
                <div className="ff-numbers">
                  <div><small>预期净值收益</small><strong><Delta value={r.expected_return}/></strong></div>
                  <div><small>相似样本</small><strong>{r.samples ?? "—"}</strong></div>
                  <div><small>置信度</small><strong>{label(r.confidence)}</strong></div>
                  <div><small>实际净值收益</small><strong><Delta value={r.realized_return}/></strong></div>
                </div>
              )}
              <div className="ff-nav">
                <NavField label="发布时最新可见净值" day={r.observed_nav_date} value={r.observed_nav} missing="无可用记录"/>
                <NavField label="核验起点净值" day={r.base_date} value={r.base_nav} missing={insufficient ? "无正式预测" : "待核对"}/>
                <NavField label="目标日净值" day={r.target_date} value={r.target_nav} missing={insufficient ? "无正式预测" : "待核对"}/>
              </div>
              {r.source && <p className="ff-period">净值来源　<SourceLink source={sourceName(r.source)} url={r.source_url} />{r.observed_at && `　·　发布时所用净值入库于 ${stamp(r.observed_at)}`}</p>}
              {r.scored_at && <p className="ff-period">核验时间 {stamp(r.scored_at)}</p>}
              <FundClock clock={r.otc}/>
              <FundFees estimate={features?.[r.code]}/>
            </div>
          </details>
        );
      })}
    </div>
  );
}
