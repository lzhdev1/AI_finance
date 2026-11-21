import React, { useEffect, useState } from "react";
import { Table } from "../components.jsx";
import { dateText, Empty, Panel, StateNotice, Tag, yuan } from "../desk.jsx";
import { num, pct, pctAbs } from "../format.js";
import useResource from "../useResource.js";
import AccountReplay, { NavComparison } from "./Replay.jsx";
import Attribution from "./ReplayAttribution.jsx";
import ReplayOpportunity from "./ReplayOpportunity.jsx";

const sections = [
  ["compare", "两版对比"],
  ["opportunity", "异动试仓"],
  ["attribution", "逐笔归因"],
  ["daily", "逐日回放"],
];
const editions = [
  ["v1", "原版"],
  ["v2", "优化版"],
];
function locationState() {
  const query = new URLSearchParams(window.location.search);
  const section = query.get("replay_tab"),
    version = query.get("version");
  return {
    section: sections.some(([id]) => section === id)
      ? section
      : version
        ? "daily"
        : "compare",
    version: version === "v2" ? "v2" : "v1",
  };
}

function joinedHistory(a, b) {
  const rows = new Map();
  for (const [summary, key] of [
    [a, "original_nav"],
    [b, "unit_nav"],
  ]) {
    for (const record of summary?.history || []) {
      const row = rows.get(record.trade_date) || {
        trade_date: record.trade_date,
      };
      row[key] = record.unit_nav;
      if (record.benchmark_return != null)
        row.benchmark_return = record.benchmark_return;
      rows.set(record.trade_date, row);
    }
  }
  return [...rows.values()].sort((x, y) =>
    x.trade_date.localeCompare(y.trade_date),
  );
}
function period(data) {
  const from = data?.period?.start,
    to = data?.history?.at(-1)?.trade_date;
  return from && to ? `${dateText(from)} 至 ${dateText(to)}` : "尚无结算记录";
}
const cashReasons = [
  ["budget_zero", "可用申购额度为零"],
  ["budget_limited", "申购额度受限，目标未成交"],
  ["hard_stop", "触发硬止损"],
  ["no_target", "当日无风险仓位目标"],
  ["other", "其他"],
];
function CashDays({ original, optimized }) {
  const accounts = [original, optimized];
  const hasData = accounts.some(
    (account) =>
      account?.cash_days > 0 &&
      cashReasons.some(([key]) => account.cash_day_reasons?.[key] != null),
  );
  if (!hasData) return null;
  return (
    <Table
      headers={["空仓日拆解（日终全现金）", "原版（天）", "优化版（天）"]}
      numeric={[1, 2]}
      rows={cashReasons.map(([key, name]) => [
        name,
        ...accounts.map((account) => account?.cash_day_reasons?.[key] ?? "—"),
      ])}
    />
  );
}
function PortfolioComparison({ original, optimized, complete }) {
  const a = original?.account,
    b = optimized?.account;
  const reconciled =
    complete &&
    [a, b].every((account) =>
      [account.gross_pnl, account.net_pnl, account.total_fees].every(
        (value) => value != null,
      ),
    );
  const signedMoney = (value) => `${value > 0 ? "+" : ""}${yuan(value)} 元`;
  const items = [
    ["模拟账本累计收益率", "cum_return", "percent"],
    ["最大回撤", "max_drawdown", "percent"],
    ["相对沪深300超额收益", "excess_return", "percent"],
    ["模拟账本申赎费（元）", "total_fees", "yuan"],
    ["费用 / 初始资产", "fee_to_capital", "percent"],
    ["平均持仓比例", "average_exposure", "percent"],
    ["空仓交易日", "cash_days", "days"],
    ["期末资产（元）", "total_value", "yuan"],
  ];
  const display = (value, type) =>
    value == null
      ? "—"
      : type === "percent"
        ? pctAbs(value, 2)
        : type === "days"
          ? value
          : yuan(value);
  const reviewed = (account, key, format) =>
    account?.fee_audit_complete === true && account[key] != null
      ? format(account[key])
      : "待核";
  const bothReviewed =
    complete && a.fee_audit_complete === true && b.fee_audit_complete === true;
  const auditRows = [
    [
      "公告费率复算费用（固定原成交，元）",
      "fixed_fill_fees",
      (v) => yuan(v, 4),
    ],
    [
      "公告费率复算收益率（固定原成交）",
      "fixed_fill_return",
      (v) => pctAbs(v, 4),
    ],
    [
      "公告费用减模拟账本费用（元）",
      "covered_fee_difference",
      (v) => yuan(v, 4),
    ],
  ].map(([name, key, format]) => {
    const diff =
      bothReviewed && a[key] != null && b[key] != null ? b[key] - a[key] : null;
    return [
      name,
      reviewed(a, key, format),
      reviewed(b, key, format),
      diff == null
        ? "待核"
        : key === "fixed_fill_return"
          ? `${pct(diff, 4).replace("%", "")} 个百分点`
          : `${diff > 0 ? "+" : ""}${format(diff)}`,
    ];
  });
  return (
    <Panel title="组合表现与成本">
      <Table
        headers={["指标", "原版", "优化版", "优化版减原版"]}
        numeric={[1, 2, 3]}
        rows={items
          .map(([name, key, type]) => {
            const left = a?.[key],
              right = b?.[key],
              v =
                left == null || right == null || !complete
                  ? null
                  : right - left;
            const difference =
              v == null
                ? "—"
                : type === "percent"
                  ? `${pct(v).replace("%", "")} 个百分点`
                  : `${v > 0 ? "+" : ""}${display(v, type)}`;
            return [
              name,
              display(left, type),
              display(right, type),
              difference,
            ];
          })
          .concat(auditRows)}
      />
      {reconciled && (
        <p className="panel-note pnl-change">
          同期净损益变化 <strong>{signedMoney(b.net_pnl - a.net_pnl)}</strong>
          ，其中持仓毛损益变化{" "}
          <strong>{signedMoney(b.gross_pnl - a.gross_pnl)}</strong>
          ，少付的模拟费用{" "}
          <strong>{signedMoney(a.total_fees - b.total_fees)}</strong>。
        </p>
      )}
      <p className="panel-note">
        {complete
          ? "差值使用两版完整的同期结果。最大回撤以负数表示。"
          : "两列分别截至各自最新结算日。两版完成相同区间后显示差值。"}{" "}
        申赎费用按模拟费率计提，费用率以初始资产为分母。
      </p>
      <p className="panel-note fee-comparison-note">
        固定成交复算沿用原交易日期、份额和净值，仅替换为公告核验的费用，后续资金与仓位路径沿用原账本。整版赎回费用核验完成后展示复算值。{" "}
        <a href="?view=replay&replay_tab=attribution&version=v1#fee-review">
          原版逐笔费差
        </a>{" "}
        ·{" "}
        <a href="?view=replay&replay_tab=attribution&version=v2#fee-review">
          优化版逐笔费差
        </a>
      </p>
      <CashDays original={a} optimized={b} />
    </Panel>
  );
}
function PredictionComparison({ original, optimized, matched, complete }) {
  const rows = [];
  for (const h of [1, 5]) {
    for (const [name, data] of [
      ["原版", original],
      ["优化版", optimized],
    ]) {
      const r = data?.forecast_stats?.find((x) => x.horizon_days === h);
      rows.push([
        `${h} 个交易日`,
        name,
        r?.total ?? "—",
        r?.scored ?? "—",
        r ? `${r.hits} / ${r.directional}` : "—",
        pctAbs(r?.hit_rate, 2),
        pctAbs(r?.baseline_up_rate, 2),
        num(r?.brier, 4),
      ]);
    }
  }
  return (
    <>
      <Panel title="各版实际发布的预测">
        <Table
          headers={[
            "预测期",
            "版本",
            "已发布",
            "已验证",
            "命中 / 方向样本",
            "命中率",
            "恒涨基线",
            "Brier 分数",
          ]}
          numeric={[2, 3, 4, 5, 6, 7]}
          rows={rows}
        />
        <p className="panel-note">
          每版自主选择研究板块。命中率统计已验证的方向判断，Brier
          分数包含中性概率。
        </p>
      </Panel>
      <Panel
        title="相同预测样本对照"
        extra={<small className="quiet">同基准日、板块、核验标的与期限</small>}
      >
        {!complete ? (
          <Empty>两版完成整个回放区间后显示配对结果。</Empty>
        ) : !matched?.length ? (
          <Empty>两版没有可配对的已验证预测。</Empty>
        ) : (
          <Table
            headers={[
              "预测期",
              "已验证配对",
              "方向配对",
              "原版命中",
              "优化版命中",
              "原版命中率",
              "优化版命中率",
              "原版 Brier",
              "优化版 Brier",
            ]}
            numeric={[1, 2, 3, 4, 5, 6, 7, 8]}
            rows={matched.map((r) => [
              `${r.horizon_days} 个交易日`,
              r.scored,
              r.directional,
              r.original_hits,
              r.optimized_hits,
              pctAbs(r.original_hit_rate, 2),
              pctAbs(r.optimized_hit_rate, 2),
              num(r.original_brier, 4),
              num(r.optimized_brier, 4),
            ])}
          />
        )}
      </Panel>
    </>
  );
}
function Comparison({ revision }) {
  const [tick, setTick] = useState(0);
  const result = useResource(
    "/api/agent/replay/comparison",
    `${revision}-${tick}`,
  );
  const data = result.data;
  const original = data?.original?.available ? data.original : null,
    optimized = data?.optimized?.available ? data.optimized : null;
  const complete =
    original?.status === "ready" &&
    optimized?.status === "ready" &&
    original.period.start === optimized.period.start &&
    original.period.end === optimized.period.end;
  useEffect(() => {
    if (result.loading || !data || complete || document.hidden) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), 15000);
    return () => window.clearTimeout(timer);
  }, [result.loading, data, complete]);
  return (
    <div className="page-stack">
      <StateNotice resource={result} />
      {data && (
        <>
          <section className="comparison-dates">
            <div>
              <Tag tone="warm">样本内比较</Tag>
              <p>优化版在复盘原版结果后，按相同历史区间重新运行。</p>
            </div>
            {[
              ["原版", original],
              ["优化版", optimized],
            ].map(([name, d]) => (
              <div key={name}>
                <strong>{name}</strong>
                <p>{period(d)}</p>
                <small>
                  {!d
                    ? "等待归档"
                    : d.status === "ready"
                      ? "已完成"
                      : `已完成 ${d.period.completed} / ${d.period.sessions} 日`}
                </small>
              </div>
            ))}
          </section>
          <NavComparison
            history={joinedHistory(original, optimized)}
            anchor={
              original?.account?.benchmark_anchor ||
              optimized?.account?.benchmark_anchor
            }
            compare
          />
          <PortfolioComparison
            original={original}
            optimized={optimized}
            complete={complete}
          />
          <PredictionComparison
            original={original}
            optimized={optimized}
            matched={data.matched_forecasts}
            complete={complete}
          />
        </>
      )}
    </div>
  );
}
export default function ReplayWorkspace({ revision }) {
  const [state, setState] = useState(locationState);
  useEffect(() => {
    const reset = () => setState(locationState());
    window.addEventListener("popstate", reset);
    return () => window.removeEventListener("popstate", reset);
  }, []);
  function navigate(section, version) {
    if (section === state.section && version === state.version) return;
    const url = new URL(window.location.href);
    url.searchParams.set("replay_tab", section);
    url.searchParams.delete("day");
    if (section === "daily" || section === "attribution") url.searchParams.set("version", version);
    else url.searchParams.delete("version");
    window.history.pushState({}, "", url);
    setState({ section, version });
  }
  return (
    <div className="page-stack">
      <div className="replay-navigation">
        <div className="desk-tabs" aria-label="回放视图">
          {sections.map(([id, name]) => (
            <button
              key={id}
              aria-pressed={state.section === id}
              className={state.section === id ? "selected" : ""}
              onClick={() => navigate(id, state.version)}
            >
              {name}
            </button>
          ))}
        </div>
        {(state.section === "daily" || state.section === "attribution") && (
          <label className="replay-filter">
            回放版本{" "}
            <select
              value={state.version}
              aria-label="回放版本"
              onChange={(e) => navigate(state.section, e.target.value)}
            >
              {editions.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {state.section === "compare" ? (
        <Comparison revision={revision} />
      ) : state.section === "opportunity" ? (
        <ReplayOpportunity revision={revision} />
      ) : state.section === "daily" ? (
        <AccountReplay
          key={state.version}
          edition={state.version}
          revision={revision}
        />
      ) : (
        <Attribution
          key={state.version}
          edition={state.version}
          revision={revision}
        />
      )}
    </div>
  );
}
