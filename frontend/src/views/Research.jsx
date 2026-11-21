import React, { useState } from "react";
import { ChevronDown, Compass, FileText, History } from "lucide-react";
import { num, pct, pctAbs, pts } from "../format.js";
import {
  dateText,
  Empty,
  label,
  modes,
  News,
  Panel,
  sourceName,
  stamp,
  StateNotice,
  Tag,
  TextBlock,
  yuan,
} from "../desk.jsx";
import { Table } from "../components.jsx";
import useResource from "../useResource.js";
import { ForecastTable, HistoryPage } from "../Archive.jsx";
import { SourceLink } from "./Market.jsx";
import BehaviorReview from "./BehaviorReview.jsx";
import FundForecasts from "./FundForecasts.jsx";

function CapitalFlow({ flow }) {
  if (!flow || flow.net_yuan == null)
    return <span>该时点没有已核实的净流入额。</span>;
  return (
    <span>
      {flow.flow_method} · 当日净流入 {yuan(flow.net_yuan)} 元 · 净流入占比{" "}
      {pts(flow.net_pct)}
      {flow.net_5d_yuan != null && (
        <>
          {" "}
          · 源站5日榜净额 {yuan(flow.net_5d_yuan)} 元
          {flow.net_5d_pct != null && `（占比 ${pts(flow.net_5d_pct)}）`}
        </>
      )}
      {" · "}
      {dateText(flow.flow_date)} ·{" "}
      <SourceLink source={flow.flow_source} url={flow.flow_url} />
    </span>
  );
}

function ToolHistory({ tools }) {
  if (!tools) return null;
  const actions = {
    inspect_sector: "查板块行情与资金",
    read_news: "查板块新闻",
    recall: "检索历史经验",
  };
  return (
    <Panel
      title="本轮工具调用"
      extra={<small className="quiet">{tools.calls?.length ?? 0} 次</small>}
    >
      {!tools.calls?.length ? (
        <Empty>{tools.status || "本轮没有工具调用记录。"}</Empty>
      ) : (
        <div className="watch-list">
          {tools.calls.map((call, i) => {
            const result = call.result || {};
            return (
              <details className="decision" key={i}>
                <summary>
                  <div>
                    <small>{stamp(call.as_of)}</small>
                    <strong>
                      {actions[call.tool] || call.tool} · {call.board}
                    </strong>
                  </div>
                  <Tag tone={call.status === "ok" ? "good" : "warm"}>
                    {call.status === "ok" ? "有结果" : "未找到记录"}
                  </Tag>
                  <ChevronDown size={16} />
                </summary>
                <div className="decision-detail">
                  {call.status === "empty" ? (
                    <p className="quiet">
                      截至本轮研究时点，没有检索到相关记录。
                    </p>
                  ) : call.tool === "read_news" ? (
                    <News items={result.articles || []} />
                  ) : call.tool === "inspect_sector" ? (
                    <>
                      <TextBlock title="行情">
                        {dateText(result.as_of)} · {sourceName(result.source)}
                        {result.proxy_code &&
                          ` · 基金代理 ${result.proxy_code}`}
                        {" · "}5 日动量 {pct(result.momentum_5)} / 20 日动量{" "}
                        {pct(result.momentum_20)}
                      </TextBlock>
                      <TextBlock title="资金流向">
                        <CapitalFlow flow={result.capital_flow} />
                      </TextBlock>
                      {result.intraday && <TextBlock title="当时可见的盘中快照">
                        {stamp(result.intraday.at)} · 盘中涨跌 {pct(result.intraday.return)}
                        {result.intraday.capital_flow?.net_yuan != null &&
                          ` · 主力净流入估算 ${yuan(result.intraday.capital_flow.net_yuan / 1e8, 2)} 亿元 · ${sourceName(result.intraday.capital_flow.source)}`}
                      </TextBlock>}
                      {result.behavior && <TextBlock title="当时的行为假设">
                        {result.behavior.label}。{result.behavior.risk_reason}
                      </TextBlock>}
                      {result.fund_status && (
                        <TextBlock title="基金映射">
                          {result.fund_status}
                        </TextBlock>
                      )}
                    </>
                  ) : (
                    <>
                      {result.memories?.length > 0 && (
                        <MemoryTable items={result.memories} />
                      )}
                      {result.prior_research?.map((r) => (
                        <TextBlock key={r.report_id} title={stamp(r.as_of)}>
                          {r.summary}{" "}
                          <a
                            className="report-link"
                            href={`?view=research&report=${r.report_id}`}
                          >
                            查看报告 #{r.report_id} ↗
                          </a>
                        </TextBlock>
                      ))}
                    </>
                  )}
                </div>
              </details>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function ResearchNote({ report }) {
  return (
    <section className="brief">
      <div className="brief-meta">
        <span>
          <FileText size={15} /> 投资研究报告
        </span>
        <Tag value={report.mode}>{modes[report.mode] || report.mode}</Tag>
      </div>
      <h2>{report.summary || "本次研究未记录摘要"}</h2>
      <div className="brief-byline">
        研究时间 {stamp(report.as_of)}
        <i />
        行情截至 {dateText(report.data_as_of)}
        {report.model && (
          <>
            <i />
            {report.model}
          </>
        )}
      </div>
      {report.summary_refs?.length > 0 && (
        <details className="evidence-fold">
          <summary>摘要引用的 {report.summary_refs.length} 项证据</summary>
          <EvidenceRefs refs={report.summary_refs} report={report} />
        </details>
      )}
      <TextBlock title="策略依据">{report.rationale}</TextBlock>
      {report.risks?.length > 0 && (
        <TextBlock title="风险与失效条件">
          <ul>
            {report.risks.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </TextBlock>
      )}
      {report.model_status && (
        <div className="model-status">{report.model_status}</div>
      )}
    </section>
  );
}

function EvidenceRefs({ refs = [], report }) {
  if (!refs?.length) return null;
  const candidates = report.evidence?.candidates || report.watchlist || [];
  const stories = [
    ...(report.evidence?.news || []),
    ...candidates.flatMap((item) => item.news || []),
  ];
  const memories =
    report.evidence?.considered_memories || report.memories || [];
  return (
    <TextBlock title="采用的证据">
      <ul>
        {refs.map((ref) => {
          const row = candidates.find((item) =>
            [
              `B${item.board_id}`,
              `I${item.board_id}`,
              `F${item.board_id}`,
              `G${item.board_id}`,
              `H${item.board_id}`,
            ].includes(ref),
          );
          const story = stories.find((item) => `N${item.id}` === ref);
          const citedFund = ref.startsWith("U") && candidates.find((c) => c.fund_outlooks?.some((f) => `U${f.code}` === ref));
          const navForecasts = (citedFund?.fund_outlooks || report.fund_forecasts || []).filter((item) => `U${item.code}` === ref);
          const memory = memories.find(
            (item) =>
              `M${item.board_id}-${item.horizon_days}` === ref &&
              candidates.some(
                (c) =>
                  c.board_id === item.board_id &&
                  c.proxy_code === item.proxy_code,
              ),
          );
          let detail = "记录未附证据描述";
          if (row && ref.startsWith("B"))
            detail = `${row.board} · ${dateText(row.as_of)} · ${sourceName(row.source)} · ${row.proxy_code ? "映射基金净值 " + row.proxy_code : "板块指数收盘价"} · 5日 ${pct(row.momentum_5)} / 20日 ${pct(row.momentum_20)}`;
          if (row?.intraday && ref.startsWith("I"))
            detail = `${row.board} · ${stamp(row.intraday.at)} · 板块盘中快照 · ${pct(row.intraday.return)}`;
          if (row?.outlooks?.length && ref.startsWith("F"))
            detail = `${row.board} · ${row.outlooks.map((f) => `${f.horizon_days}日上涨概率 ${pctAbs(f.prob_up, 1)}、预期收益 ${pct(f.expected_return)}`).join(" / ")}。${row.forecast_note || ""}`;
          if (row?.behavior && ref.startsWith("H"))
            detail = `${row.board} · ${row.behavior.label}（待验证） · ${row.behavior.risk_reason}`;
          if (navForecasts.length)
            detail = `${navForecasts[0].name}（${navForecasts[0].code}） · 本基金单位净值 · ${navForecasts.map((f) =>
              f.prob_up == null ? `${f.horizon_days} 日未形成预测` : `${f.horizon_days} 日上涨概率 ${pctAbs(f.prob_up, 1)}`
            ).join(" / ")}`;
          if (row && ref.startsWith("G"))
            detail = (
              <>
                {row.board} · <CapitalFlow flow={row.capital_flow} />
              </>
            );
          if (story)
            detail = `${stamp(story.published_at)} · ${sourceName(story.source)} · ${story.title}`;
          if (memory)
            detail = `${memory.horizon_days}个交易日 · ${memory.samples} 条已验证记录 · ${memory.lesson}`;
          return (
            <li key={ref}>
              <code>{ref}</code> {detail}
            </li>
          );
        })}
      </ul>
    </TextBlock>
  );
}

function Watchlist({ items = [], report }) {
  if (!items.length) return <Empty>本次研究没有选定跟踪板块。</Empty>;
  return (
    <div className="watch-list">
      {items.map((row) => (
        <details key={row.board} className="watch-row">
          <summary>
            <span className="small-icon">
              <Compass size={17} />
            </span>
            <div>
              <strong>{row.board}</strong>
              <small>{row.reason || "选择理由未记录"}</small>
            </div>
            {row.intent && (
              <Tag value={row.intent}>
                {row.intent === "observe" ? "只跟踪" : "评估配置"}
              </Tag>
            )}
            <ChevronDown size={16} />
          </summary>
          <div className="watch-detail">
            <div className="submetrics">
              {Object.entries({
                综合评分: num(row.score),
                "5日动量": pct(row.momentum_5),
                "20日动量": pct(row.momentum_20),
                资金流比率: pct(row.flow_ratio),
              }).map(([name, value]) => (
                <div key={name}>
                  <small>{name}</small>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <TextBlock title="行情口径">
              {row.proxy_code
                ? `映射基金净值代理 ${row.proxy_code}`
                : "板块指数收盘价"}{" "}
              · {dateText(row.as_of)} · {sourceName(row.source)}
            </TextBlock>
            {row.capital_flow && (
              <TextBlock
                title={
                  row.evidence_ids?.includes(`G${row.board_id}`)
                    ? `可核查的资金证据 G${row.board_id}`
                    : "资金流向"
                }
              >
                <CapitalFlow flow={row.capital_flow} />
              </TextBlock>
            )}
            {row.intraday && (
              <TextBlock title="盘中快照">
                {stamp(row.intraday.at)} · 涨跌幅 {pct(row.intraday.return)}
              </TextBlock>
            )}
            <EvidenceRefs refs={row.refs} report={report} />
            <TextBlock title="预测对配置的影响">{row.forecast_note}</TextBlock>
            <TextBlock title="关联基金">
              {(row.funds || []).length
                ? row.funds
                    .map(
                      (fund) =>
                        `${fund.name}（${fund.code}）${fund.mapping_source === "name-match" ? " · 名称匹配" : ""}`,
                    )
                    .join("、")
                : row.fund_status || "本时点无可用基金映射。"}
            </TextBlock>
            <TextBlock title="研究引用的新闻">
              <News items={row.news} />
            </TextBlock>
          </div>
        </details>
      ))}
    </div>
  );
}

function Targets({ report }) {
  const rows = report.targets && Object.entries(report.targets);
  const names = Object.fromEntries(
    (report.watchlist || []).flatMap((row) =>
      (row.funds || []).map((fund) => [fund.code, fund.name]),
    ),
  );
  return (
    <Panel
      title="建议目标仓位"
      extra={<small className="quiet">研究时点</small>}
    >
      {rows?.length ? (
        <Table
          headers={["基金代码", "基金", "目标权重"]}
          numeric={[2]}
          rows={rows
            .sort((a, b) => b[1] - a[1])
            .map(([code, weight]) => [
              code,
              names[code] || "—",
              pctAbs(weight, 1),
            ])}
        />
      ) : (
        <Empty>
          {report.targets == null
            ? "本次研究没有给出可执行的目标组合。"
            : "本次研究的目标组合为现金。"}
        </Empty>
      )}
    </Panel>
  );
}

export function MemoryTable({ items = [] }) {
  return items.length ? (
    <Table
      headers={[
        "板块 / 核验标的",
        "预测期",
        "方向样本",
        "命中率",
        "平均实际收益",
        "Brier 分数",
        "经验摘要",
      ]}
      numeric={[2, 3, 4, 5]}
      rows={items.map((row) => [
        <div className="cell-stack">
          <b>{row.board}</b>
          <small>
            {row.proxy_code
              ? `基金净值代理 ${row.proxy_code}`
              : "板块指数收盘价"}
          </small>
        </div>,
        `${row.horizon_days} 个交易日`,
        row.samples,
        pctAbs(row.hit_rate, 1),
        pct(row.mean_return),
        num(row.brier, 3),
        row.lesson,
      ])}
    />
  ) : (
    <Empty>已完成验证的板块预测将汇总为经验记录。</Empty>
  );
}

export default function Research({ data, revision }) {
  const [id, setId] = useState(
    () => new URLSearchParams(window.location.search).get("report") || "",
  );
  const [archive, setArchive] = useState(false);
  function selectReport(next) {
    setId(String(next));
    setArchive(false);
    const url = new URL(window.location.href);
    if (next) url.searchParams.set("report", next);
    else url.searchParams.delete("report");
    window.history.replaceState({}, "", url);
  }
  const past = useResource(
    id ? `/api/agent/dashboard?report_id=${encodeURIComponent(id)}` : null,
  );
  const report = id ? past.data?.report : data.report;
  return (
    <div className="page-stack">
      <div className="toolbar">
        <label>
          <History size={16} />
          研究档案
          <select value={id} onChange={(e) => selectReport(e.target.value)}>
            <option value="">最新报告</option>
            {id &&
              !(data.reports || []).some((row) => String(row.id) === id) && (
                <option value={id}>历史报告 #{id}</option>
              )}
            {(data.reports || []).map((row) => (
              <option key={row.id} value={row.id}>
                {stamp(row.as_of)} · {modes[row.mode] || row.mode} · #{row.id}
              </option>
            ))}
          </select>
        </label>
        <button
          className="control"
          aria-expanded={archive}
          onClick={() => setArchive((n) => !n)}
        >
          {archive ? "收起档案" : "浏览全部报告"}
        </button>
      </div>
      {archive && (
        <Panel title="研究档案">
          <HistoryPage kind="reports" revision={revision}>
            {(rows) =>
              rows.length ? (
                <Table
                  headers={["时间", "研究方式", "研究摘要", ""]}
                  rows={rows.map((row) => [
                    stamp(row.as_of),
                    modes[row.mode] || row.mode,
                    row.summary,
                    <button
                      className="more"
                      onClick={() => selectReport(row.id)}
                    >
                      查看 #{row.id}
                    </button>,
                  ])}
                />
              ) : (
                <Empty>尚无研究报告。</Empty>
              )
            }
          </HistoryPage>
        </Panel>
      )}
      {id && <StateNotice resource={past} />}
      {report ? (
        <>
          <ResearchNote report={report} />
          <BehaviorReview key={report.id} items={report.watchlist} published={report.forecasts} />
          <ToolHistory tools={report.evidence?.research_tools} />
          <div className="research-grid">
            <Panel
              title="跟踪板块"
              extra={
                <span className="quantity">
                  {report.watchlist?.length || 0}
                </span>
              }
            >
              <Watchlist items={report.watchlist} report={report} />
            </Panel>
            <Targets report={report} />
          </div>
          <Panel title="本次板块预测">
            <ForecastTable rows={report.forecasts} />
            {report.evidence?.forecast_method && (
              <p className="panel-note">{report.evidence.forecast_method}</p>
            )}
          </Panel>
          {report.fund_forecasts?.length > 0 && (
            <Panel title="关注基金的净值预测" extra={<a className="report-link" href="?view=forecasts&pred_tab=funds">全部基金预测 ↗</a>}>
              <FundForecasts rows={report.fund_forecasts} features={report.evidence?.fund_features} reportView />
            </Panel>
          )}
          <Panel
            title="研究时点的证据"
            extra={
              <small className="quiet">
                净值日 {dateText(report.evidence?.nav_date)}
              </small>
            }
          >
            <div className="panel-inner">
              <p className="quiet">
                行情样本覆盖 {report.evidence?.market_count ?? "—"} 个板块
              </p>
              {report.evidence?.notes?.map((line, i) => (
                <p className="data-note" key={i}>
                  {line}
                </p>
              ))}
              {report.evidence?.candidates?.length > 0 && (
                <details className="evidence-fold">
                  <summary>
                    本次比较的候选板块（{report.evidence.candidates.length}）
                  </summary>
                  <Watchlist
                    items={report.evidence.candidates}
                    report={report}
                  />
                </details>
              )}
              {report.evidence?.holdings?.length > 0 && (
                <details className="evidence-fold">
                  <summary>研究时的组合持仓</summary>
                  <Table
                    headers={["基金", "代码", "仓位", "日期"]}
                    numeric={[2]}
                    rows={report.evidence.holdings.map((h) => [
                      h.name,
                      h.code,
                      pctAbs(h.weight, 1),
                      dateText(h.trade_date),
                    ])}
                  />
                </details>
              )}
              {report.evidence?.recent_orders?.length > 0 && (
                <details className="evidence-fold">
                  <summary>本次参考的成交记录</summary>
                  <Table
                    headers={[
                      "成交日期",
                      "基金",
                      "方向",
                      "成交金额（元）",
                      "费用（元）",
                    ]}
                    numeric={[3, 4]}
                    rows={report.evidence.recent_orders.map((o) => [
                      dateText(o.execute_on),
                      o.name,
                      label(o.side),
                      yuan(o.amount),
                      yuan(o.fee),
                    ])}
                  />
                </details>
              )}
              <News items={report.evidence?.news} />
            </div>
          </Panel>
          <Panel title="本次研究引用的经验">
            <MemoryTable items={report.memories} />
          </Panel>
          {report.evidence?.prior_research?.length > 0 && (
            <Panel title="本次调用的过往研究">
              <div className="panel-inner">
                {report.evidence.prior_research.map((row, i) => (
                  <TextBlock key={i} title={stamp(row.as_of)}>
                    {row.summary}
                  </TextBlock>
                ))}
              </div>
            </Panel>
          )}
        </>
      ) : (
        (!id || (!past.loading && !past.error)) && (
          <Panel title="研究报告">
            <Empty>
              尚无研究报告。点击「运行研究」可使用已入库的资料发起一次分析。
            </Empty>
          </Panel>
        )
      )}
      {!id && (
        <Panel
          title="最近采集的新闻"
          extra={<small className="quiet">按发布时间</small>}
        >
          <div className="panel-inner">
            <News items={data.news} />
          </div>
        </Panel>
      )}
    </div>
  );
}
