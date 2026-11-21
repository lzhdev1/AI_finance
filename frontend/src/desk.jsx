import React from "react";
import { ArrowRight, Inbox, LoaderCircle } from "lucide-react";
import { cls, isNil, pct } from "./format.js";

export const modes = {
  llm: "模型研究",
  rules: "规则研究",
  insufficient: "证据不足",
};
const labels = {
  pending: "待验证",
  scored: "已验证",
  running: "运行中",
  ok: "已完成",
  success: "已完成",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
  error: "异常",
  partial: "部分完成",
  degraded: "已降级",
  queued: "排队中",
  open: "建仓",
  add: "加仓",
  reduce: "减仓",
  rebalance: "调仓",
  close: "清仓",
  hold: "持有",
  buy: "申购",
  sell: "赎回",
  low: "低",
  mid: "中",
  high: "高",
  active: "运行中",
  retired: "已退役",
  research: "投研分析",
  manual_research: "手动研究",
  premarket_news: "盘前资讯",
  intraday_decide: "盘中决策",
  intraday_snapshot: "盘中采样",
  ingest_market: "收盘采集",
  settle: "模拟结算",
  score: "预测评分",
  pipeline: "调度任务",
  order: "模拟成交",
  news: "资讯采集",
  forecast: "板块预测",
  decision: "策略决策",
  memory: "经验回顾",
  run: "调度任务",
  lifecycle: "账户事件",
  created: "已创建",
  opened: "已开立",
  agent_research: "经理研究",
  manager_decision: "经理决策",
  manager_settle: "经理组合结算",
  market_snapshot: "市场快照",
  market_watch: "全天市场观察",
  fund_nav: "基金净值采集",
  board_quote: "板块行情采集",
  board_flow: "板块资金流采集",
  index_quote: "指数行情采集",
  calendar: "交易日历更新",
  market_flow: "市场资金流采集",
  trade_calendar: "交易日历更新",
  index_daily: "指数日线采集",
  morning_nav_retry: "早间净值补录",
  late_nav_retry: "晚间净值补录",
  decision_retry: "盘中决策重试",
  settled: "已结算",
  waiting_nav: "等待净值",
  blocked: "暂缓结算",
  review: "复核历史预测",
  analysis: "分析市场证据",
  finished: "已结束",
  paused: "已暂停",
  activated: "已启用",
  boards: "板块目录",
  board_flow_industry: "行业资金流采集",
  board_flow_concept: "概念资金流采集",
  fund_mapping: "基金与板块映射",
  forecast_review: "预测复盘",
  index_000001: "上证指数采集",
  index_000300: "沪深300采集",
  index_399001: "深证成指采集",
  index_399006: "创业板指采集",
};
export const label = (key) => labels[key] || key || "—";
const publishers = {
  ths: "同花顺",
  eastmoney: "东方财富",
  "eastmoney-pingzhong": "东方财富 · 天天基金单位净值",
  cls: "财联社",
  sina: "新浪财经",
  "chinanews-finance": "中新网财经",
};
export const sourceName = (name) => publishers[name] || name || "来源未记录";
export const dateText = (value) => (value ? String(value).slice(0, 10) : "—");
const stampFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
export function stamp(value) {
  if (!value) return "—";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : stampFmt.format(date);
}
export function yuan(value, digits = 2) {
  if (isNil(value)) return "—";
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}
export function Delta({ value }) {
  return <span className={cls(value)}>{pct(value)}</span>;
}
export function Empty({ children, loading = false }) {
  const Icon = loading ? LoaderCircle : Inbox;
  return (
    <div className="empty" role="status">
      <Icon size={20} className={loading ? "spin" : ""} />
      <span>{children}</span>
    </div>
  );
}
export function StateNotice({ resource }) {
  if (resource.error)
    return (
      <p className="alert" role="alert">
        {resource.data
          ? "刷新失败，上次读取的记录仍显示在下方。"
          : "数据暂时无法读取。"}{" "}
        {resource.error}
      </p>
    );
  if (resource.data == null && resource.loading)
    return <Empty loading>正在读取记录…</Empty>;
  return null;
}
export function Panel({ title, extra, children, className = "" }) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <h2>{title}</h2>
        {extra}
      </header>
      {children}
    </section>
  );
}
export function More({ children, onClick }) {
  return (
    <button className="more" onClick={onClick}>
      {children}
      <ArrowRight size={14} />
    </button>
  );
}
export function Tag({ value, children, tone }) {
  const skin =
    tone ||
    {
      ok: "good",
      success: "good",
      completed: "good",
      scored: "good",
      llm: "good",
      partial: "warm",
      running: "warm",
      pending: "warm",
      error: "bad",
      failed: "bad",
      insufficient: "warm",
    }[value] ||
    "";
  return <span className={`tag ${skin}`}>{children || label(value)}</span>;
}
export function TextBlock({ title, children }) {
  if (!children) return null;
  return (
    <section className="text-block">
      <h3>{title}</h3>
      <div>{children}</div>
    </section>
  );
}
export function News({ items = [] }) {
  if (!items.length) return <Empty>当前记录没有附带新闻。</Empty>;
  return (
    <ul className="news-list">
      {items.map((item, index) => {
        const safe =
          typeof item.url === "string" && /^https?:\/\//i.test(item.url);
        return (
          <li key={item.id ?? item.news_id ?? index}>
            {safe ? (
              <a href={item.url} target="_blank" rel="noreferrer">
                {item.title}
                <span aria-hidden="true"> ↗</span>
              </a>
            ) : (
              <strong>{item.title}</strong>
            )}
            <small>
              {sourceName(item.source)} · {stamp(item.published_at)}
            </small>
            {(item.excerpt || item.body) &&
              (item.excerpt || item.body) !== item.title && (
                <details className="news-copy">
                  <summary>查看入库正文</summary>
                  <p>{item.excerpt || item.body}</p>
                </details>
              )}
          </li>
        );
      })}
    </ul>
  );
}
