import React, { useEffect, useRef, useState } from "react";
import { Activity, CalendarDays, ChevronDown, ListFilter } from "lucide-react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Table } from "../components.jsx";
import {
  dateText,
  Delta,
  Empty,
  label,
  modes,
  Panel,
  stamp,
  StateNotice,
  Tag,
  TextBlock,
  yuan,
} from "../desk.jsx";
import { isNil, num, pct, pctAbs } from "../format.js";
import useResource from "../useResource.js";
import "../replay.css";

const actions = [
  ["", "全部动作"],
  ["open", "建仓"],
  ["add", "加仓"],
  ["reduce", "减仓"],
  ["rebalance", "调仓"],
  ["hold", "持有"],
  ["close", "清仓"],
];

export function NavComparison({ history, anchor, compare = false }) {
  const [range, setRange] = useState(0);
  const records =
    range === -1
      ? history.filter(
          (row) => row.original_nav != null && row.unit_nav != null,
        )
      : range
        ? history.slice(-range)
        : history;
  const points = records.map((row) => ({
    day: row.trade_date,
    manager:
      row.unit_nav ?? (isNil(row.cum_return) ? null : 1 + row.cum_return),
    benchmark: isNil(row.benchmark_return) ? null : 1 + row.benchmark_return,
    original: row.original_nav ?? null,
  }));
  if (range <= 0 && anchor && points.length) {
    points.unshift({
      day: anchor,
      manager: points.some((p) => p.manager != null) ? 1 : null,
      original: compare && points.some((p) => p.original != null) ? 1 : null,
      benchmark: 1,
    });
  }
  return (
    <section className="performance replay-curve">
      <header>
        <div className="dark-title">
          <Activity size={18} />
          <h2>{compare ? "两版净值与沪深300" : "净值走势"}</h2>
        </div>
        <div className="chart-tabs" aria-label="回放图表范围">
          {[
            [0, "全期"],
            ...(compare ? [[-1, "共同区间"]] : []),
            [30, "近30期"],
          ].map(([n, text]) => (
            <button
              key={n}
              aria-pressed={range === n}
              className={range === n ? "selected" : ""}
              onClick={() => setRange(n)}
            >
              {text}
            </button>
          ))}
        </div>
      </header>
      {points.length ? (
        <div className="replay-plot">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              data={points}
              margin={{ top: 12, right: 8, bottom: 2, left: 0 }}
            >
              <defs>
                <linearGradient
                  id="replay-nav-fill"
                  x1="0"
                  x2="0"
                  y1="0"
                  y2="1"
                >
                  <stop offset="0%" stopColor="#c4eca0" stopOpacity={0.22} />
                  <stop offset="100%" stopColor="#c4eca0" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                vertical={false}
                stroke="#425147"
                strokeDasharray="3 5"
              />
              <XAxis
                dataKey="day"
                tickFormatter={(v) => String(v).slice(5)}
                minTickGap={23}
                tickMargin={7}
                tick={{ fill: "#c0cec1", fontSize: 10 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                type="number"
                domain={["auto", "auto"]}
                tickFormatter={(v) => num(v, 3)}
                width={44}
                tick={{ fill: "#c0cec1", fontSize: 10 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                formatter={(value, key) => [
                  num(value, 4),
                  key === "manager"
                    ? compare
                      ? "优化版"
                      : "回放组合"
                    : key === "original"
                      ? "原版"
                      : "沪深300",
                ]}
                contentStyle={{
                  background: "#fdfefa",
                  border: "none",
                  borderRadius: 6,
                  fontSize: 12,
                  color: "#24382c",
                }}
                itemStyle={{ color: "#24382c" }}
              />
              <Area
                type="linear"
                dataKey="manager"
                stroke="#c4eca0"
                strokeWidth={2.3}
                fill="url(#replay-nav-fill)"
                dot={points.length === 1}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="original"
                stroke="#e4a07e"
                strokeWidth={2}
                hide={!compare}
                dot={points.length === 1}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="benchmark"
                stroke="#b8c5d0"
                strokeDasharray="4 4"
                dot={points.length === 1}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <Empty>所选期间没有账户净值记录。</Empty>
      )}
      <div className="plot-legend">
        <span>
          <i />
          {compare ? "优化版" : "回放组合"}
        </span>
        <span>
          <i />
          沪深300
        </span>
        {compare && (
          <span>
            <i style={{ background: "#e4a07e" }} />
            原版
          </span>
        )}
        <small>期初净值为 1</small>
      </div>
    </section>
  );
}

function ForecastStats({ items = [] }) {
  return (
    <Panel
      title="预测验证"
      extra={<small className="quiet">按预测期限独立评分</small>}
    >
      {items.length ? (
        <Table
          headers={[
            "期限",
            "发布",
            "已验证",
            "未到期",
            "到期缺行情",
            "中性",
            "方向命中 / 样本",
            "方向命中率",
            "恒涨基线",
            "Brier 分数",
          ]}
          numeric={[1, 2, 3, 4, 5, 6, 7, 8, 9]}
          rows={items.map((row) => [
            `${row.horizon_days} 个交易日`,
            row.total ?? "—",
            row.scored ?? "—",
            row.not_due ?? "—",
            row.overdue ?? "—",
            row.neutral ?? "—",
            `${row.hits ?? "—"} / ${row.directional ?? "—"}`,
            pctAbs(row.hit_rate, 1),
            pctAbs(row.baseline_up_rate, 1),
            num(row.brier, 3),
          ])}
        />
      ) : (
        <Empty>本次回放尚无预测统计。</Empty>
      )}
      <p className="panel-note">
        命中率统计已验证的方向判断。恒涨基线是在同一批样本中每次都预测上涨的命中率。Brier
        分数统计全部已验证概率，越低越好。
        {items.some((r) => r.post_verified > 0) &&
          items
            .filter((r) => r.post_verified > 0)
            .map(
              (r) =>
                ` ${r.horizon_days} 日有 ${r.post_verified} 条使用原指数期后补核，仅计入最终成绩。`,
            )}
      </p>
    </Panel>
  );
}

function DisclosureLink({ url, title }) {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url))
    return <span>{title}</span>;
  return (
    <a href={url} target="_blank" rel="noreferrer">
      {title} ↗
    </a>
  );
}

function FundDisclosures({ materials }) {
  const [selected, setSelected] = useState("");
  const funds = materials?.funds || [];
  const fund = funds.find((item) => item.code === selected) || funds[0];
  if (!fund) return null;
  return (
    <details className="replay-method fund-materials">
      <summary>基金与跟踪指数（{funds.length} 份资料）</summary>
      <div className="fund-materials-body">
        <strong className="fund-disclosure-label">历史资料核对</strong>
        <p>{materials.scope} 逐日决策采用的材料见该日证据。</p>
        <label className="fund-select">
          查看基金
          <select
            value={fund.code}
            onChange={(e) => setSelected(e.target.value)}
          >
            {funds.map((item) => (
              <option key={item.code} value={item.code}>
                {item.mapped_sector} · {item.code} ·{" "}
                {item.historical_share_name}
              </option>
            ))}
          </select>
        </label>
        <dl className="fund-facts">
          {[
            ["研究板块", fund.mapped_sector],
            ["披露时份额名称", fund.historical_share_name],
            ["基金代码", fund.code],
            ["实际跟踪指数", fund.underlying_index],
            ["公告日期", dateText(fund.disclosed)],
          ].map(([name, value]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd>{value || "—"}</dd>
            </div>
          ))}
        </dl>
        <DisclosureLink
          url={fund.source_url}
          title={fund.title || "查看产品资料原文"}
        />
        {materials.scope_notes?.length > 0 && (
          <section>
            <h3>核对范围</h3>
            <ul>
              {materials.scope_notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          </section>
        )}
        {materials.effective_changes?.length > 0 && (
          <details className="fund-changes">
            <summary>
              产品及指数变更（{materials.effective_changes.length}）
            </summary>
            <ol>
              {materials.effective_changes.map((change) => (
                <li key={`${change.code}-${change.effective}`}>
                  <strong>
                    {change.code} · {dateText(change.effective)} 生效
                  </strong>
                  <p>{change.description}</p>
                  <DisclosureLink
                    url={change.url}
                    title={`${dateText(change.published)} 公告原文`}
                  />
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </details>
  );
}

function Coverage({ coverage = {}, sessions, limitations = [], disclosures }) {
  return (
    <Panel title="数据覆盖与口径">
      <dl className="replay-coverage">
        {[
          [
            "研究材料含新闻的交易日",
            `${coverage.news_days ?? "—"} / ${sessions ?? "—"}`,
          ],
          ["研究材料去重新闻", `${coverage.news_items ?? "—"} 条`],
          ["归档新闻标题", `${coverage.source_news_items ?? "—"} 条`],
          ["归档自然日（含预热）", `${coverage.source_news_days ?? "—"} 天`],
          ["模型参与研究", `${coverage.model_days ?? "—"} 天`],
          ["规则或降级研究", `${coverage.rule_days ?? "—"} 天`],
          ["已确认模拟成交", `${coverage.orders ?? "—"} 笔`],
        ].map(([name, value]) => (
          <div key={name}>
            <dt>{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <FundDisclosures materials={disclosures} />
      {limitations.length > 0 && (
        <details className="replay-method">
          <summary>数据来源、假设与限制（{limitations.length}）</summary>
          <ul className="replay-notes">
            {limitations.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  );
}

function DayEvidence({ items = [] }) {
  if (!items.length) return <p className="quiet">该日没有记录可引用的证据。</p>;
  return (
    <ul className="news-list">
      {items.map((item, i) => (
        <li key={i}>
          {typeof item.url === "string" && /^https?:\/\//i.test(item.url) ? (
            <a href={item.url} target="_blank" rel="noreferrer">
              {item.title} ↗
            </a>
          ) : (
            <strong>{item.title}</strong>
          )}
          <small>
            {item.cited != null && (
              <Tag tone={item.cited ? "good" : ""}>
                {item.cited ? "已引用" : "已查阅"}
              </Tag>
            )}{" "}
            {item.id && <code>{item.id} · </code>}
            {item.kind === "market" ? "收盘行情" : label(item.kind)} ·{" "}
            {item.source || "来源未记录"}
            {item.published_at && ` · ${stamp(item.published_at)}`}
          </small>
        </li>
      ))}
    </ul>
  );
}
function forecastResult(forecast, through) {
  if (forecast.hit != null) return forecast.hit ? "方向命中" : "方向未命中";
  if (forecast.score_status === "scored") return "已验证 · 方向中性";
  if (forecast.target_date && through)
    return forecast.target_date > through ? "未到期" : "到期缺行情";
  return label(forecast.score_status);
}

function DayRecord({ day, through, initiallyExpanded }) {
  const [expanded, setExpanded] = useState(Boolean(initiallyExpanded));
  const {
    trade_date,
    action,
    summary,
    rationale,
    evidence,
    targets,
    instructions,
    orders,
    forecasts,
  } = day;
  return (
    <details
      className="replay-day"
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <time>{dateText(trade_date)}</time>
        <Tag value={action}>{label(action)}</Tag>
        <strong>{summary || "当日决策记录"}</strong>
        <small>{orders?.length || 0} 笔成交</small>
        <ChevronDown size={16} />
      </summary>
      {expanded && (
        <div className="replay-day-body">
          <div className="replay-byline">
            {day.mode && (
              <Tag value={day.mode}>{modes[day.mode] || day.mode}</Tag>
            )}
            {day.model && <span>{day.model}</span>}
            {day.as_of && <span>决策时点 {stamp(day.as_of)}</span>}
            {day.data_as_of && (
              <span>已知收盘日 {dateText(day.data_as_of)}</span>
            )}
          </div>
          <div className="submetrics">
            {[
              ["结算净值", num(day.unit_nav, 4)],
              ["结算资产（元）", yuan(day.total_value)],
              ["账面现金（元）", yuan(day.cash)],
              ["目标现金权重", pctAbs(day.target_cash, 1)],
            ].map(([name, value]) => (
              <div key={name}>
                <small>{name}</small>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <TextBlock title="当日研究判断">{summary}</TextBlock>
          <TextBlock title="组合与交易依据">{rationale}</TextBlock>
          <TextBlock title="模型使用情况">{day.model_status}</TextBlock>
          {day.watchlist?.length > 0 && (
            <TextBlock title="跟踪板块">
              <Table
                headers={[
                  "板块",
                  "意图",
                  "5日动量",
                  "20日动量",
                  "跟踪依据 / 引用",
                ]}
                numeric={[2, 3]}
                rows={day.watchlist.map((w) => [
                  w.sector_name,
                  w.intent === "allocate"
                    ? "配置候选"
                    : w.intent === "observe"
                      ? "观察"
                      : "—",
                  <Delta value={w.momentum_5} />,
                  <Delta value={w.momentum_20} />,
                  <div className="cell-stack">
                    {w.reason || "—"}
                    <small>{w.refs?.join(" · ")}</small>
                  </div>,
                ])}
              />
            </TextBlock>
          )}
          {[
            ["风险判断", day.risks],
            ["风控检查", day.risk_checks],
            ["资料与方法", day.notes],
          ].map(
            ([name, items]) =>
              items?.length > 0 && (
                <TextBlock key={name} title={name}>
                  <ul>
                    {items.map((item, i) => (
                      <li key={i}>{item}</li>
                    ))}
                  </ul>
                </TextBlock>
              ),
          )}
          <TextBlock title="当日证据">
            <DayEvidence items={evidence} />
          </TextBlock>
          {day.research_targets != null && (
            <TextBlock title="确认与费用风控前的研究目标">
              {Object.keys(day.research_targets).length ? (
                <Table
                  headers={["基金代码", "研究目标权重"]}
                  numeric={[1]}
                  rows={Object.entries(day.research_targets).map(
                    ([code, weight]) => [code, pctAbs(weight, 1)],
                  )}
                />
              ) : (
                "研究目标为全现金。"
              )}
            </TextBlock>
          )}
          <TextBlock
            title={
              day.research_targets != null ? "风控后的执行目标" : "目标配置"
            }
          >
            {targets?.length ? (
              <Table
                headers={["板块", "基金代码", "目标权重", "纳入理由"]}
                numeric={[2]}
                rows={targets.map((t) => [
                  t.sector_name || "—",
                  t.ts_code || "—",
                  pctAbs(t.target_weight, 1),
                  t.reason || "—",
                ])}
              />
            ) : (
              "该日没有记录基金目标权重。"
            )}
          </TextBlock>
          <div className="replay-detail-grid">
            <TextBlock title="申赎指令">
              {instructions?.length ? (
                <Table
                  headers={[
                    "方向",
                    "基金代码",
                    "冻结申购额（元）",
                    "冻结赎回份额",
                    "参考净值",
                    "说明",
                  ]}
                  numeric={[2, 3, 4]}
                  rows={instructions.map((o) => [
                    label(o.action),
                    o.ts_code,
                    o.action === "buy" ? yuan(o.amount) : "—",
                    o.action === "sell" ? yuan(o.shares, 4) : "—",
                    num(o.est_nav, 4),
                    o.reason || "—",
                  ])}
                />
              ) : (
                "该日没有申赎指令。"
              )}
            </TextBlock>
            <TextBlock title="确认成交">
              {orders?.length ? (
                <Table
                  headers={[
                    "方向",
                    "基金代码",
                    "份额",
                    "单位净值",
                    "金额（元）",
                    "费用（元）",
                    "成交说明",
                  ]}
                  numeric={[2, 3, 4, 5]}
                  rows={orders.map((o) => [
                    label(o.action),
                    o.ts_code,
                    yuan(o.shares, 4),
                    num(o.nav, 4),
                    yuan(o.amount),
                    yuan(o.fee),
                    o.reason || "—",
                  ])}
                />
              ) : (
                "该日没有确认成交。"
              )}
            </TextBlock>
          </div>
          <TextBlock title="结算说明">{day.settlement_summary}</TextBlock>
          <TextBlock title="当日预测与验证">
            {forecasts?.length ? (
              <Table
                headers={[
                  "板块 / 核验标的",
                  "期限 / 目标日",
                  "上涨概率",
                  "实际收益",
                  "验证结果",
                ]}
                numeric={[2, 3]}
                rows={forecasts.map((f) => [
                  <div className="cell-stack">
                    {f.sector_name}
                    <small>
                      {f.proxy_code
                        ? `映射基金净值 ${f.proxy_code}`
                        : "板块指数收盘价"}
                    </small>
                  </div>,
                  <div className="cell-stack">
                    {f.horizon_days} 个交易日
                    <small>{dateText(f.target_date)}</small>
                  </div>,
                  <div className="cell-stack">
                    {pctAbs(f.up_probability, 1)}
                    {!isNil(f.expected_return) && (
                      <small>
                        预期 {pct(f.expected_return)} ·{" "}
                        {f.all_samples == null
                          ? `${f.samples ?? "—"} 个样本`
                          : `同状态样本 ${f.samples ?? "—"} / 全部历史窗口 ${f.all_samples}`}
                      </small>
                    )}
                    {f.calibration && (
                      <small>
                        原始 {pctAbs(f.raw_prob_up, 1)} · 校准偏移{" "}
                        {pct(f.calibration.shift, 2).replace("%", "")} 个百分点
                        · {f.calibration.days} 日
                        {f.calibration.latest &&
                          `（最近 ${dateText(f.calibration.latest)}）`}
                      </small>
                    )}
                  </div>,
                  <Delta value={f.actual_return} />,
                  f.after_replay_verified ? (
                    <div className="cell-stack">
                      {forecastResult(f, through)}
                      <small>
                        <a
                          href={f.verification_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          期后补核
                        </a>{" "}
                        · 收盘 {num(f.base_close, 3)} → {num(f.target_close, 3)}
                      </small>
                    </div>
                  ) : (
                    forecastResult(f, through)
                  ),
                ])}
              />
            ) : (
              "该日没有发布可计分的板块预测。"
            )}
          </TextBlock>
          {day.holdings_before?.length > 0 && (
            <details className="evidence-fold">
              <summary>决策前的基金持仓</summary>
              <Table
                headers={["基金", "代码", "权重", "日期"]}
                numeric={[2]}
                rows={day.holdings_before.map((h) => [
                  h.name,
                  h.code,
                  pctAbs(h.weight, 1),
                  dateText(h.trade_date),
                ])}
              />
            </details>
          )}
          {day.memories?.length > 0 && (
            <details className="evidence-fold">
              <summary>决策时可用的板块经验</summary>
              <Table
                headers={["板块", "期限", "方向样本", "命中率", "经验记录"]}
                numeric={[2, 3]}
                rows={day.memories.map((m) => [
                  m.board,
                  `${m.horizon_days} 个交易日`,
                  m.samples,
                  pctAbs(m.hit_rate, 1),
                  m.lesson,
                ])}
              />
            </details>
          )}
        </div>
      )}
    </details>
  );
}
function DailyJournal({ revision, counts = {}, completed, through, edition }) {
  const [action, setAction] = useState("");
  const [dayFilter, setDayFilter] = useState(
    () => new URLSearchParams(location.search).get("day") || "",
  );
  const [page, setPage] = useState(0);
  const limit = 20;
  const query = new URLSearchParams({
    version: edition,
    offset: page * limit,
    limit,
    action: action || "all",
  });
  if (dayFilter) query.set("day", dayFilter);
  const resource = useResource(`/api/agent/replay/days?${query}`, revision);
  const records = resource.data;
  const journalRef = useRef(null);
  const didScroll = useRef(false);
  useEffect(() => {
    if (
      records?.items?.length &&
      dayFilter &&
      !didScroll.current &&
      window.location.hash === "#replay-days"
    ) {
      didScroll.current = true;
      journalRef.current?.scrollIntoView({ block: "start" });
    }
  }, [records, dayFilter]);
  return (
    <div id="replay-days" ref={journalRef}>
      <Panel
        title="逐日决策记录"
        extra={
          <div className="day-filters">
            <label className="replay-filter">
              日期
              <input
                type="date"
                aria-label="查看指定回放日期"
                value={dayFilter}
                onChange={(e) => {
                  const value = e.target.value;
                  setDayFilter(value);
                  setPage(0);
                  const url = new URL(window.location.href);
                  if (value) url.searchParams.set("day", value);
                  else url.searchParams.delete("day");
                  window.history.replaceState({}, "", url);
                }}
              />
            </label>
            <label className="replay-filter">
              <ListFilter size={14} />
              <select
                aria-label="按回放动作筛选"
                value={action}
                onChange={(e) => {
                  setAction(e.target.value);
                  setPage(0);
                }}
              >
                {actions.map(([value, name]) => (
                  <option key={value} value={value}>
                    {name}
                    {`（${value ? counts[value] || 0 : completed}）`}
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
      >
        <StateNotice resource={resource} />
        {records &&
          (records.items.length ? (
            <div className="replay-days">
              {records.items.map((day) => (
                <DayRecord
                  key={`${day.trade_date}-${dayFilter}`}
                  day={day}
                  through={through}
                  initiallyExpanded={Boolean(dayFilter)}
                />
              ))}
            </div>
          ) : (
            <Empty>所选动作没有回放记录。</Empty>
          ))}
        {(records || page > 0) && (
          <div className="pager">
            <span>
              第 {page + 1} 页{records && ` · 共 ${records.total} 个交易日`}
            </span>
            <button disabled={!page} onClick={() => setPage((p) => p - 1)}>
              上一页
            </button>
            <button
              disabled={!records?.has_more}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>
        )}
      </Panel>
    </div>
  );
}

export default function Replay({ revision, edition = "v1" }) {
  const [tick, setTick] = useState(0);
  const version = `${revision}-${tick}`;
  const replay = useResource(`/api/agent/replay?version=${edition}`, version);
  const data = replay.data;
  useEffect(() => {
    if (data?.status !== "running" || replay.loading) return;
    const timer = window.setTimeout(() => setTick((value) => value + 1), 10000);
    return () => window.clearTimeout(timer);
  }, [data?.status, replay.loading, tick]);
  return (
    <div className="page-stack">
      <StateNotice resource={replay} />
      {data &&
        (!data.available || !data.account ? (
          <Panel title="历史回放账户">
            <Empty loading={data.status === "running"}>
              {data.status === "running"
                ? `正在运行半年回放，已完成 ${data.period?.completed ?? 0} 个交易日。`
                : "尚无已生成的半年回放记录。"}
            </Empty>
          </Panel>
        ) : (
          <>
            <div className="replay-range">
              <CalendarDays size={16} />
              <strong>
                {dateText(data.period.start)} — {dateText(data.period.end)}
              </strong>
              <span>
                {data.period.sessions} 个交易日 · 回放账户 #{data.id}
                {data.status === "running" &&
                  data.history?.length > 0 &&
                  ` · 已结算至 ${dateText(data.history.at(-1).trade_date)}`}
              </span>
              <Tag value={data.status === "ready" ? "ok" : "running"}>
                {data.status === "ready"
                  ? "回放完成"
                  : `已完成 ${data.period.completed} 日`}
              </Tag>
            </div>
            <div className="metrics">
              {[
                [
                  data.status === "ready"
                    ? "期末回放资产（元）"
                    : "已结算回放资产（元）",
                  yuan(data.account.total_value),
                  `初始资金 ${yuan(data.account.initial_capital, 0)} 元`,
                ],
                [
                  "组合收益率",
                  pct(data.account.cum_return),
                  `沪深300 ${pct(data.account.benchmark_return)}`,
                ],
                [
                  "组合最大回撤",
                  pctAbs(data.account.max_drawdown),
                  `沪深300 ${pctAbs(data.account.benchmark_max_drawdown)}`,
                ],
                [
                  "超额收益（百分点）",
                  pct(data.account.excess_return).replace("%", ""),
                  `账面现金 ${yuan(data.account.cash, 0)} 元`,
                ],
              ].map(([name, value, detail]) => (
                <div className="metric" key={name}>
                  <div className="metric-title">{name}</div>
                  <div className="metric-number">{value}</div>
                  <div className="metric-note">{detail}</div>
                </div>
              ))}
            </div>
            <div className="overview-grid replay-top">
              <NavComparison
                history={data.history || []}
                anchor={data.account.benchmark_anchor}
              />
              <Coverage
                coverage={data.coverage}
                sessions={data.period.sessions}
                limitations={data.limitations}
                disclosures={data.fund_disclosures}
              />
            </div>
            <ForecastStats items={data.forecast_stats} />
            <DailyJournal
              edition={edition}
              key={data.id}
              revision={version}
              counts={data.coverage?.actions}
              completed={data.period.completed}
              through={data.history?.at(-1)?.trade_date}
            />
          </>
        ))}
    </div>
  );
}
