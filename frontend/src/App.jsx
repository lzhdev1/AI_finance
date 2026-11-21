import React, { useEffect, useState } from "react";
import {
  Activity,
  ArrowRight,
  BookOpen,
  BriefcaseBusiness,
  ChartCandlestick,
  ChartNoAxesCombined,
  ChevronRight,
  CircleDot,
  Clock3,
  Fingerprint,
  FlaskConical,
  LayoutDashboard,
  Landmark,
  Menu,
  PanelLeftClose,
  Play,
  RefreshCw,
  History,
  X,
} from "lucide-react";
import { request } from "./api.js";
import { stamp, StateNotice } from "./desk.jsx";
import useResource from "./useResource.js";
import Overview from "./views/Overview.jsx";
import Research from "./views/Research.jsx";
import Replay from "./views/ReplayWorkspace.jsx";
import Market from "./views/Market.jsx";
import {
  DecisionHistory,
  Forecasts,
  Ledger,
  Memory,
  Journal,
} from "./views/Records.jsx";

const pages = [
  {
    id: "overview",
    name: "经理总览",
    icon: LayoutDashboard,
    tabs: [
      ["overview", "账户概览"],
      ["journal", "运行日志"],
    ],
  },
  { id: "market", name: "市场与资金", icon: Landmark },
  {
    id: "research",
    name: "投研与决策",
    icon: BookOpen,
    tabs: [
      ["research", "研究报告"],
      ["decisions", "决策指令"],
      ["memory", "经验记忆"],
    ],
  },
  { id: "forecasts", name: "板块与基金预测", icon: ChartCandlestick },
  { id: "ledger", name: "组合账本", icon: BriefcaseBusiness },
  { id: "replay", name: "策略回放", icon: History },
];
const oldViews = {
  pods: "ledger",
  cards: "decisions",
  preds: "forecasts",
  factors: "memory",
};
const parentPage = (key) =>
  pages.find(
    (page) => page.id === key || page.tabs?.some(([id]) => id === key),
  );
function readView() {
  const key = new URLSearchParams(window.location.search).get("view");
  return parentPage(key) ? key : oldViews[key] || "overview";
}
const today = (value) =>
  new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(value ? new Date(value) : new Date());

export default function App() {
  const [view, setView] = useState(readView);
  const [mobile, setMobile] = useState(false);
  const [revision, setRevision] = useState(0);
  const [post, setPost] = useState(false);
  const [message, setMessage] = useState(null);
  const dashboard = useResource("/api/agent/dashboard", revision);
  const data = dashboard.data;
  const running = data?.runs?.some(
    (run) => run.status === "running" && /research/.test(run.phase),
  );
  const timerMs = running || message?.pending ? 5000 : 30000;

  useEffect(() => {
    // Wait for the preceding read before starting another polling interval.
    if (dashboard.loading || view === "replay") return;
    const timer = window.setInterval(() => {
      if (!document.hidden) setRevision((n) => n + 1);
    }, timerMs);
    return () => clearInterval(timer);
  }, [dashboard.loading, timerMs, view]);
  useEffect(() => {
    const back = () => {
      setView(readView());
      setMobile(false);
    };
    window.addEventListener("popstate", back);
    return () => window.removeEventListener("popstate", back);
  }, []);
  useEffect(() => {
    if (!message?.pending || !data) return;
    const run = data.runs?.find(
      (row) =>
        /research/.test(row.phase) &&
        (message.runId != null
          ? String(row.run_id) === String(message.runId)
          : Number(row.run_id) > message.lastRunId),
    );
    if (run && !["queued", "running"].includes(run.status)) {
      const failed = ["error", "failed"].includes(run.status);
      const text = failed
        ? `研究运行失败。${run.error || "可在运行日志查看记录。"}`
        : run.status === "partial"
          ? `本次研究已结束。${run.stats?.model_status || "已记录本轮降级原因。"}`
          : "本次研究已结束，可在投研与决策中查看结果。";
      setMessage({ text, bad: failed || run.status === "partial" });
    }
  }, [data, message]);

  function go(key, params = {}) {
    setMobile(false);
    if (view === key) return;
    const url = new URL(window.location.href);
    url.search = new URLSearchParams({ view: key, ...params }).toString();
    url.hash = "";
    window.history.pushState({}, "", url);
    setView(key);
    window.scrollTo(0, 0);
  }
  async function runResearch() {
    if (post || running) return;
    setPost(true);
    try {
      const result = await request("/api/agent/research", { method: "POST" });
      setMessage({
        text: "研究任务已提交，正在等待运行结果。",
        pending: true,
        runId: result?.run_id,
        lastRunId: Math.max(
          0,
          ...(data?.runs || []).map((row) => Number(row.run_id)),
        ),
      });
    } catch (error) {
      setMessage({
        text:
          error.status === 409
            ? "已有研究任务正在运行，进度可在运行日志查看。"
            : `提交研究失败。${error.message}`,
        bad: error.status !== 409,
      });
    } finally {
      setPost(false);
      setRevision((n) => n + 1);
    }
  }
  const page = parentPage(view);
  const online = data?.status?.scheduler_online;
  const nav = pages.map((item) => (
    <button
      key={item.id}
      onClick={() => go(item.id)}
      aria-label={item.name}
      title={item.name}
      aria-current={page.id === item.id ? "page" : undefined}
      className={page.id === item.id ? "desk-nav active" : "desk-nav"}
    >
      <item.icon size={18} />
      <span>{item.name}</span>
      {page.id === item.id && <ChevronRight size={14} className="nav-arrow" />}
    </button>
  ));

  return (
    <div className="desk-shell">
      {mobile && (
        <button
          className="veil"
          aria-label="关闭导航"
          onClick={() => setMobile(false)}
        />
      )}
      <aside className={`desk-side ${mobile ? "visible" : ""}`}>
        <div className="side-brand">
          <span className="brandmark">
            <ChartNoAxesCombined size={24} strokeWidth={2.5} />
          </span>
          <div>
            <b>QuantLab</b>
            <small>基金经理工作台</small>
          </div>
          <button
            className="side-close"
            aria-label="关闭导航"
            onClick={() => setMobile(false)}
          >
            <PanelLeftClose size={18} />
          </button>
        </div>
        <div className="side-rule" />
        <nav aria-label="主导航">{nav}</nav>
        <div className="side-bottom">
          <div className="agent-avatar">
            <Fingerprint size={20} />
          </div>
          <div>
            <strong>基金经理 Agent</strong>
            <small>
              <i className={`status-dot ${online === true ? "on" : ""}`} />
              {online == null
                ? "调度状态未知"
                : online
                  ? "调度器在线"
                  : "未收到近期心跳"}
            </small>
          </div>
        </div>
      </aside>
      <div className="desk-main">
        <div className="desk-top">
          <div className="breadcrumbs">
            <button
              className="menu-open"
              onClick={() => setMobile(true)}
              aria-label="打开导航"
            >
              <Menu size={20} />
            </button>
            <span className="bread-label">工作台</span>
            <ChevronRight size={14} />
            <strong>{page.name}</strong>
          </div>
          <div className="environment">
            <FlaskConical size={14} /> 模拟环境
          </div>
        </div>
        <main className="desk-content">
          <header className="page-head">
            <div>
              <div className="dateline">
                <Clock3 size={13} />
                {today(data?.status?.now)} · 上海
              </div>
              <h1>{page.name}</h1>
            </div>
            <div className="head-actions">
              <button
                className="control"
                onClick={() => setRevision((n) => n + 1)}
                disabled={dashboard.loading}
                aria-label="刷新工作台"
              >
                <RefreshCw
                  size={15}
                  className={dashboard.loading ? "spin" : ""}
                />
                <span>刷新</span>
              </button>
              {["overview", "journal", "research"].includes(view) && (
                <button
                  className="primary"
                  onClick={runResearch}
                  disabled={post || running || message?.pending || !data}
                >
                  <Play size={14} fill="currentColor" />
                  {post
                    ? "提交中"
                    : running || message?.pending
                      ? "研究中"
                      : "运行研究"}
                </button>
              )}
            </div>
          </header>
          {message && (
            <div
              className={`notice ${message.bad ? "warn" : ""}`}
              role="status"
            >
              <CircleDot size={16} />
              <span>{message.text}</span>
              <button onClick={() => go("journal")}>
                运行日志 <ArrowRight size={13} />
              </button>
              <button onClick={() => setMessage(null)} aria-label="关闭提示">
                <X size={15} />
              </button>
            </div>
          )}
          {page.tabs && (
            <nav
              className="workspace-sections"
              aria-label={page.name + "子导航"}
            >
              {page.tabs.map(([id, name]) => (
                <button
                  key={id}
                  aria-current={id === view ? "page" : undefined}
                  onClick={() => go(id)}
                >
                  {name}
                </button>
              ))}
            </nav>
          )}
          {view === "replay" ? (
            <Replay revision={revision} />
          ) : view === "market" ? (
            <Market revision={revision} />
          ) : view === "forecasts" ? (
            <Forecasts revision={revision} />
          ) : (
            <StateNotice resource={dashboard} />
          )}
          {data && (
            <>
              {view === "overview" && <Overview data={data} go={go} />}
              {view === "research" && (
                <Research data={data} revision={revision} />
              )}
              {view === "decisions" && <DecisionHistory revision={revision} />}
              {view === "ledger" && <Ledger data={data} revision={revision} />}
              {view === "memory" && <Memory data={data} />}
              {view === "journal" && (
                <Journal data={data} revision={revision} />
              )}
            </>
          )}
          <footer className="desk-footer">
            <span>
              <Activity size={13} /> 最近同步 {stamp(data?.status?.now)}
            </span>
            <span>QuantLab · 基金经理工作台</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
