import React, { useState } from "react";
import {
  Activity,
  ArrowDownUp,
  Banknote,
  CircleDot,
  Compass,
  Layers,
  PieChart,
  TrendingUp,
  Wallet,
} from "lucide-react";
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
import { cls, isNil, pct, pctAbs } from "../format.js";
import {
  dateText,
  Delta,
  Empty,
  label,
  modes,
  More,
  Panel,
  stamp,
  Tag,
  yuan,
} from "../desk.jsx";

function Metric({ name, value, unit, note, icon: Icon, color }) {
  return (
    <div className="metric">
      <div className="metric-title">
        <span>{name}</span>
        <Icon size={16} />
      </div>
      <div className={`metric-number ${color || ""}`}>
        {value}
        {unit && value !== "—" && <small>{unit}</small>}
      </div>
      <div className="metric-note">{note}</div>
    </div>
  );
}

export function Performance({ manager }) {
  const [range, setRange] = useState(30);
  const history = manager?.history || [];
  const data = (range ? history.slice(-range) : history).filter(
    (item) => !isNil(item.cum_return),
  );
  return (
    <section className="performance">
      <header>
        <div className="dark-title">
          <Activity size={18} />
          <h2>组合表现</h2>
        </div>
        <div className="chart-tabs" aria-label="图表范围">
          {[
            [7, "7期"],
            [30, "30期"],
            [0, "全部"],
          ].map(([n, text]) => (
            <button
              key={n}
              className={range === n ? "selected" : ""}
              onClick={() => setRange(n)}
              aria-pressed={range === n}
            >
              {text}
            </button>
          ))}
        </div>
      </header>
      <div className="chart-meta">
        <span>累计收益率</span>
        <strong>{pct(manager?.cum_return)}</strong>
      </div>
      {data.length ? (
        <div className="performance-plot">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              data={data}
              margin={{ top: 6, right: 6, bottom: 0, left: -5 }}
            >
              <defs>
                <linearGradient id="return-shade" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#c4eca0" stopOpacity={0.25} />
                  <stop offset="100%" stopColor="#c4eca0" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                vertical={false}
                stroke="#425147"
                strokeDasharray="3 5"
              />
              <XAxis
                dataKey="trade_date"
                minTickGap={28}
                tickMargin={9}
                axisLine={false}
                tickLine={false}
                tick={{ fill: "#b0bdb5", fontSize: 10 }}
                tickFormatter={(v) => String(v).slice(5)}
              />
              <YAxis
                tickFormatter={(v) => pctAbs(v, 1)}
                width={54}
                tick={{ fill: "#b0bdb5", fontSize: 10 }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                formatter={(value, key) => [
                  pct(value),
                  key === "cum_return" ? "经理组合" : "沪深300",
                ]}
                contentStyle={{
                  color: "#1a3025",
                  border: "0",
                  borderRadius: 5,
                  fontSize: 12,
                }}
                itemStyle={{ color: "#1a3025" }}
              />
              <Area
                type="linear"
                dataKey="cum_return"
                stroke="#c4eca0"
                strokeWidth={2.5}
                fill="url(#return-shade)"
                dot={data.length === 1}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="benchmark_return"
                stroke="#9baab2"
                strokeDasharray="4 4"
                dot={data.length === 1}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="plot-empty">
          <Activity size={24} />
          <span>等待首个结算净值</span>
          <small>模拟结算后展示经理组合和沪深300的累计收益。</small>
        </div>
      )}
      <div className="plot-legend">
        <span>
          <i />
          经理组合
        </span>
        <span>
          <i />
          沪深300
        </span>
        <small>
          {data.length
            ? `${dateText(data[0].trade_date)} – ${dateText(data.at(-1).trade_date)}`
            : "尚无结算日期"}
        </small>
      </div>
    </section>
  );
}

function Momentum({ report }) {
  const [term, setTerm] = useState(5);
  const field = `momentum_${term}`;
  const rows = report.watchlist.filter((row) => !isNil(row[field]));
  const extent = Math.max(0.001, ...rows.map((row) => Math.abs(row[field])));
  return (
    <section className="performance momentum">
      <header>
        <div className="dark-title">
          <Activity size={18} />
          <h2>跟踪板块动量</h2>
        </div>
        <div className="chart-tabs" aria-label="动量周期">
          {[5, 20].map((n) => (
            <button
              key={n}
              className={term === n ? "selected" : ""}
              aria-pressed={term === n}
              onClick={() => setTerm(n)}
            >
              {n}日
            </button>
          ))}
        </div>
      </header>
      <div className="momentum-sub">
        近 {term} 个交易日累计涨跌幅 <span>{dateText(report.data_as_of)}</span>
      </div>
      <div className="momentum-rows">
        {rows.map((row) => (
          <div key={row.board} className="momentum-row">
            <div>
              <strong>{row.board}</strong>
              <small>
                {row.proxy_code ? `基金代理 ${row.proxy_code}` : "板块指数"}
              </small>
            </div>
            <div className="bar-track">
              <i
                className={row[field] < 0 ? "neg" : "pos"}
                style={{ width: `${(Math.abs(row[field]) / extent) * 50}%` }}
              />
            </div>
            <b>{pct(row[field])}</b>
          </div>
        ))}
      </div>
      <div className="momentum-key">
        <span>
          <i />
          上涨
        </span>
        <span>
          <i />
          下跌
        </span>
        <small>以收盘数据计量</small>
      </div>
    </section>
  );
}

export function Events({ items = [], compact = false }) {
  if (!items.length) return <Empty>尚未记录运行或模拟交易事件。</Empty>;
  return (
    <ol className={`events ${compact ? "compact" : ""}`}>
      {items.map((event, i) => (
        <li key={`${event.ts}-${event.kind}-${i}`}>
          <span className="event-icon">
            {event.kind === "order" ? (
              <ArrowDownUp size={13} />
            ) : event.kind === "decision" ? (
              <Compass size={13} />
            ) : (
              <Activity size={13} />
            )}
          </span>
          <div className="event-body">
            <div className="event-head">
              <strong>{label(event.title || event.kind)}</strong>
              {event.status && <Tag value={event.status} />}
            </div>
            {event.detail && (
              <div className="event-detail">{label(event.detail)}</div>
            )}
            <small>
              {stamp(event.ts)} · {label(event.kind)}
              {event.run_id != null ? ` · #${event.run_id}` : ""}
            </small>
          </div>
        </li>
      ))}
    </ol>
  );
}

export default function Overview({ data, go }) {
  const { manager, report } = data;
  const watchlist = report?.watchlist || [];
  const cashPct =
    manager?.total_value > 0 && !isNil(manager.cash)
      ? pctAbs(manager.cash / manager.total_value, 1)
      : "—";
  return (
    <div className="page-stack">
      <div className="metrics">
        <Metric
          name="模拟总资产"
          value={yuan(manager?.total_value)}
          unit="元"
          icon={Wallet}
          note={`最近结算  ${dateText(manager?.last_date)}`}
        />
        <Metric
          name="账面现金"
          value={yuan(manager?.cash)}
          unit="元"
          icon={Banknote}
          note={`占总资产  ${cashPct}`}
        />
        <Metric
          name="累计收益"
          value={pct(manager?.cum_return)}
          color={cls(manager?.cum_return)}
          icon={TrendingUp}
          note={`回撤  ${pct(manager?.drawdown)}`}
        />
        <Metric
          name="经理重点跟踪"
          value={report ? watchlist.length : "—"}
          unit="个"
          icon={Layers}
          note={
            report?.evidence?.market_count != null
              ? `本轮扫描 ${report.evidence.market_count} 个板块`
              : `研究数据日  ${dateText(report?.data_as_of)}`
          }
        />
      </div>
      <div className="overview-grid">
        {!manager?.history?.length &&
        watchlist.some(
          (row) => !isNil(row.momentum_5) && !isNil(row.momentum_20),
        ) ? (
          <Momentum report={report} />
        ) : (
          <Performance manager={manager} />
        )}
        <Panel
          title="研究焦点"
          extra={<More onClick={() => go("research")}>查看研究</More>}
        >
          {report ? (
            <div className="focus">
              <div className="focus-label">
                <CircleDot size={14} /> 最新研究 <span>·</span>{" "}
                {dateText(report.as_of)}
              </div>
              <p className="focus-summary">
                {report.summary || "本次研究未记录摘要。"}
              </p>
              <div className="focus-tags">
                <Tag value={report.mode}>
                  {modes[report.mode] || report.mode}
                </Tag>
                {report.model && (
                  <span className="model-id">{report.model}</span>
                )}
              </div>
              {watchlist.length > 0 && (
                <div className="watch-chips">
                  {watchlist.map((row) => (
                    <span key={row.board}>{row.board}</span>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="focus-empty">
              <Compass size={28} />
              <strong>等待首份研究报告</strong>
              <p>可运行研究，或等调度器完成下一次分析。</p>
            </div>
          )}
        </Panel>
      </div>
      <div className="overview-grid lower">
        <Panel
          title="最近行为"
          extra={<More onClick={() => go("journal")}>运行日志</More>}
        >
          <Events items={(data.events || []).slice(0, 4)} compact />
        </Panel>
        <Panel
          title="经理最新发布的预测"
          extra={
            <More onClick={() => go("forecasts", { pred_tab: "published" })}>
              查看发布记录
            </More>
          }
        >
          {report?.forecasts?.length ? (
            <div className="forecast-preview">
              {report.forecasts.slice(0, 5).map((row) => (
                <div key={row.id ?? `${row.board}-${row.horizon_days}`}>
                  <span className="forecast-icon">
                    <PieChart size={15} />
                  </span>
                  <div>
                    <strong>{row.board}</strong>
                    <small>
                      {row.horizon_days} 个交易日 ·{" "}
                      {row.proxy_code
                        ? `基金代理 ${row.proxy_code}`
                        : "板块指数"}{" "}
                      · {label(row.status)}
                    </small>
                  </div>
                  <span>
                    <b>{pctAbs(row.prob_up, 0)}</b>
                    <small>上涨概率</small>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>
              尚无板块预测。有效行情样本齐备后，研究报告将记录预测。
            </Empty>
          )}
        </Panel>
      </div>
    </div>
  );
}
