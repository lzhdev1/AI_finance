import React, { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import { Table } from "../components.jsx";
import {
  dateText,
  Delta,
  Empty,
  label,
  Panel,
  StateNotice,
  Tag,
  yuan,
} from "../desk.jsx";
import { cls, num, pct, pctAbs } from "../format.js";
import useResource from "../useResource.js";

const safeUrl = (value) =>
  typeof value === "string" && /^https?:\/\//i.test(value);
const tabNames = [
  ["lots", "FIFO 配对复盘"],
  ["orders", "申赎流水"],
  ["forecasts", "预测诊断"],
];
function Money({ value }) {
  const displayed = value != null && Math.abs(value) < 0.005 ? 0 : value;
  return <span className={cls(displayed)}>{yuan(displayed)}</span>;
}
function Facts({ items }) {
  return (
    <dl className="attribution-facts">
      {items.map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}
function FeeReview({ audit, documents = [], onOrder }) {
  if (!audit) return null;
  const comparable = audit.covered_orders > 0;
  return (
    <div className="fee-review">
      <h3>个人投资者场外 C 类公告费率事后核验</h3>
      <p className="quiet">
        按赎回日已披露的费率、FIFO
        持有天数、成交份额和净值复算。费差仅统计有适用公告的赎回。
      </p>
      <Facts
        items={[
          [
            "公告覆盖 / 已平仓批次",
            `${audit.covered_lots ?? "—"} / ${audit.closed_lots ?? "—"}`,
          ],
          [
            "已覆盖 / 未覆盖赎回单",
            `${audit.covered_orders ?? "—"} / ${audit.uncovered_orders ?? "—"}`,
          ],
          ["成交时采用缺省费率的订单", audit.fallback_orders],
          [
            "覆盖订单账本费用（元）",
            comparable ? yuan(audit.covered_booked_fees, 4) : "—",
          ],
          [
            "按公告复算费用（元）",
            comparable ? yuan(audit.covered_disclosed_fees, 4) : "—",
          ],
          [
            "公告减账本差额（元）",
            comparable ? yuan(audit.covered_difference, 4) : "—",
          ],
          ["未覆盖批次", audit.uncovered_lots],
          ["未覆盖订单账本费用（元）", yuan(audit.uncovered_booked_fees, 4)],
        ]}
      />
      {audit.uncovered_codes?.length > 0 && (
        <p className="fee-note">
          基金 {audit.uncovered_codes.join("、")}{" "}
          的赎回日适用公告尚待核验。这些订单保留账本扣费。
        </p>
      )}
      {audit.changed_orders?.length > 0 && (
        <Table
          headers={[
            "差异订单 / 赎回日",
            "基金代码",
            "账本费用（元）",
            "公告复算（元）",
            "公告减账本（元）",
          ]}
          numeric={[2, 3, 4]}
          rows={audit.changed_orders.map((order) => [
            <div className="cell-stack">
              <button
                className="replay-link"
                onClick={() => onOrder(order.order_id)}
              >
                #{order.order_id} 查看流水
              </button>
              <small>{dateText(order.date)}</small>
            </div>,
            order.code,
            yuan(order.booked_fee, 4),
            yuan(order.disclosed_fee, 4),
            yuan(order.difference, 4),
          ])}
        />
      )}
      {documents.length > 0 && (
        <details className="fund-changes">
          <summary>核验使用的管理人公告（{documents.length} 份）</summary>
          <Table
            maxHeight={400}
            headers={["基金 / 公告日", "持有自然日与费率", "公告原文及页码"]}
            rows={documents.map((doc) => [
              <div className="cell-stack">
                {doc.code}
                <small>{dateText(doc.published)}</small>
              </div>,
              <div className="fee-rates">
                {(doc.rates || []).map(([end, rate], i) => {
                  const begin = i ? doc.rates[i - 1][0] : 0;
                  const term =
                    end == null
                      ? begin
                        ? `满${begin}天`
                        : "全部期限"
                      : begin
                        ? `${begin}至${end - 1}天`
                        : `不足${end}天`;
                  return (
                    <span key={i}>
                      {term} {pctAbs(rate, 2)}
                    </span>
                  );
                })}
              </div>,
              safeUrl(doc.url) ? (
                <a
                  href={
                    doc.page
                      ? `${doc.url.split("#")[0]}#page=${doc.page}`
                      : doc.url
                  }
                  target="_blank"
                  rel="noreferrer"
                >
                  {doc.title}
                  {doc.page && ` · 第${doc.page}页`} ↗
                </a>
              ) : (
                doc.title
              ),
            ])}
          />
        </details>
      )}
    </div>
  );
}
function Sources({ summary: s, audit, feeDocuments, onOrder }) {
  const hasFeeReview = s.short_redemption_fees != null || Boolean(audit);
  useEffect(() => {
    if (!hasFeeReview || window.location.hash !== "#fee-review") return;
    const review = document.getElementById("fee-review");
    if (review) {
      review.open = true;
      review.scrollIntoView({ block: "start" });
    }
  }, [hasFeeReview]);
  return (
    <Panel title="盈亏来源" extra={<small className="quiet">人民币元</small>}>
      <Facts
        items={[
          ["持仓期间市场贡献", <Money value={s.market_pnl} />],
          ["板块选择贡献", <Money value={s.selection_pnl} />],
          ["基金跟踪差异", <Money value={s.tracking_pnl} />],
          ["缺数据未归因", <Money value={s.unattributed_pnl} />],
          ["舍入影响", <Money value={s.rounding_pnl} />],
          ["账本核对差额", yuan(s.reconciliation_difference, 4)],
        ]}
      />
      <details className="replay-method">
        <summary>查看市场暴露与基准的差异</summary>
        <Facts
          items={[
            ["期初资金全额持有沪深300", <Money value={s.full_market_pnl} />],
            [
              "仓位与持仓时段产生的差异",
              <Money value={s.exposure_timing_pnl} />,
            ],
            ["持仓期间市场贡献", <Money value={s.market_pnl} />],
          ]}
        />
        <p className="panel-note">
          这里将持仓期间的市场贡献与全期满仓基准比较。两部分差异共同反映现金仓位和进出场时段。
        </p>
      </details>
      {hasFeeReview && (
        <details className="replay-method" id="fee-review">
          <summary>短期赎回费用与批次损益{audit && " · 公告核验"}</summary>
          <Facts
            items={[
              ["短期赎回订单", s.short_redemption_orders],
              ["涉及的FIFO批次", s.short_redemption_lots],
              ["短期赎回费（元）", yuan(s.short_redemption_fees)],
              [
                "占全部申赎费用",
                s.fees > 0 ? pctAbs(s.short_redemption_fees / s.fees, 1) : "—",
              ],
              ["盈利 / 亏损批次", `${s.profitable_lots} / ${s.losing_lots}`],
              ["盈利合计（元）", <Money value={s.total_profits} />],
              ["亏损合计（元）", <Money value={s.total_losses} />],
            ]}
          />
          <FeeReview audit={audit} documents={feeDocuments} onOrder={onOrder} />
        </details>
      )}
      {s.window_total_lots != null && (
        <details className="replay-method">
          <summary>同板块、同持有期限的事后对照</summary>
          <Facts
            items={[
              [
                "完整窗口覆盖批次",
                `${s.window_covered_lots} / ${s.window_total_lots}`,
              ],
              ["典型板块超额（元）", <Money value={s.typical_selection_pnl} />],
              ["实际选时偏离（元）", <Money value={s.selection_timing_pnl} />],
              ["典型市场影响（元）", <Money value={s.typical_market_pnl} />],
              ["实际市场时段偏离（元）", <Money value={s.market_timing_pnl} />],
            ]}
          />
          <p className="panel-note">
            在回放区间内，对同一板块、相同持仓期限且行情完整的窗口等权比较。这项事后拆解用于描述进场日期的影响；上方实际盈亏已包含该影响。
          </p>
        </details>
      )}
    </Panel>
  );
}
function Method({ lines }) {
  return lines?.length ? (
    <details className="replay-method attr-method">
      <summary>计算方法与归因边界</summary>
      <ul className="replay-notes">
        {lines.map((line, i) => (
          <li key={i}>{line}</li>
        ))}
      </ul>
    </details>
  ) : null;
}
function OrderLink({ id, onClick }) {
  return id == null ? (
    "—"
  ) : (
    <button className="replay-link" onClick={() => onClick(id)}>
      #{id} 查看流水
    </button>
  );
}
function DecisionLink({ day, edition }) {
  return day ? (
    <a
      href={`?view=replay&replay_tab=daily&version=${edition}&day=${encodeURIComponent(day)}#replay-days`}
    >
      {dateText(day)} · 当日决策
    </a>
  ) : (
    "—"
  );
}
function Lot({ lot, disclosure, onOrder, edition }) {
  const [open, setOpen] = useState(false);
  const closed = lot.status === "closed";
  const mapping =
    lot.mapping_source === "name-match" ? "基金名称匹配" : lot.mapping_source;
  return (
    <details
      className="attr-lot"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <time>{dateText(lot.entry_date)}</time>
        <Tag value={closed ? "ok" : "pending"}>
          {closed ? "已平仓" : "持仓中"}
        </Tag>
        <strong>
          {lot.name || lot.code}
          <small>
            {lot.code} · {lot.board || "板块未映射"}
          </small>
        </strong>
        <div className="lot-profit">
          <Money value={lot.net_pnl} />
          <small>净盈亏（元）</small>
        </div>
        <ChevronDown size={15} />
      </summary>
      {open && (
        <div className="lot-detail">
          <h3>买入与{closed ? "赎回" : "期末估值"}配对</h3>
          <Facts
            items={[
              ["配对编号", lot.lot_id],
              [
                "申购成交",
                <OrderLink id={lot.buy_order_id} onClick={onOrder} />,
              ],
              [
                "赎回成交",
                <OrderLink id={lot.sell_order_id} onClick={onOrder} />,
              ],
              [
                "买入日",
                <DecisionLink day={lot.entry_date} edition={edition} />,
              ],
              [
                closed ? "赎回日" : "估值日",
                closed ? (
                  <DecisionLink day={lot.exit_date} edition={edition} />
                ) : (
                  dateText(lot.mark_date)
                ),
              ],
              [
                "持有时长",
                `${lot.holding_days ?? "—"} 天 / ${lot.holding_sessions ?? "—"} 个交易日`,
              ],
              ["配对份额", yuan(lot.shares, 4)],
              ["买入净值", num(lot.entry_nav, 4)],
              [closed ? "赎回净值" : "估值净值", num(lot.exit_nav, 4)],
              ["配对成本（元）", yuan(lot.cost_basis)],
              [
                closed ? "赎回金额（元）" : "估值金额（元）",
                yuan(lot.proceeds),
              ],
              ["毛盈亏（元）", <Money value={lot.gross_pnl} />],
              ["申购费（元）", yuan(lot.buy_fee)],
              ["赎回费（元）", yuan(lot.sell_fee)],
              ["净盈亏（元）", <Money value={lot.net_pnl} />],
            ]}
          />
          <h3>板块与基金来源</h3>
          <Facts
            items={[
              ["研究板块", lot.board],
              ["映射来源", mapping],
              ["映射依据", lot.mapping_basis],
              ["映射冻结日", dateText(lot.mapping_date)],
              ["资料披露的标的指数", disclosure?.underlying_index],
              [
                "产品公告",
                safeUrl(disclosure?.source_url) ? (
                  <a
                    href={disclosure.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {dateText(disclosure.disclosed)} 原文 ↗
                  </a>
                ) : (
                  "—"
                ),
              ],
            ]}
          />
          <h3>持有期表现</h3>
          <Facts
            items={[
              ["基金收益率", <Delta value={lot.fund_return} />],
              ["映射板块收益率", <Delta value={lot.board_return} />],
              ["沪深300收益率", <Delta value={lot.benchmark_return} />],
              ["市场贡献（元）", <Money value={lot.market_pnl} />],
              ["板块选择（元）", <Money value={lot.selection_pnl} />],
              ["基金跟踪（元）", <Money value={lot.tracking_pnl} />],
              ["未归因（元）", <Money value={lot.unattributed_pnl} />],
              ["舍入（元）", <Money value={lot.rounding_pnl} />],
            ]}
          />
          <h3>进出场时点复盘</h3>
          {lot.window_count != null && (
            <Facts
              items={[
                ["同期限可比窗口", lot.window_count],
                [
                  "窗口平均板块相对收益",
                  <Delta value={lot.mean_sector_excess_return} />,
                ],
                [
                  "典型板块超额（元）",
                  <Money value={lot.typical_selection_pnl} />,
                ],
                [
                  "实际选时偏离（元）",
                  <Money value={lot.selection_timing_pnl} />,
                ],
                [
                  "窗口平均沪深300收益",
                  <Delta value={lot.mean_market_return} />,
                ],
                [
                  "典型市场影响（元）",
                  <Money value={lot.typical_market_pnl} />,
                ],
                [
                  "实际市场时段偏离（元）",
                  <Money value={lot.market_timing_pnl} />,
                ],
              ]}
            />
          )}
          <Facts
            items={[
              [
                "入场前5个交易日基金涨跌",
                <Delta value={lot.entry_prior_5d_return} />,
              ],
              ["入场前窗口起点", dateText(lot.prior_window_start)],
              [
                "离场后5个交易日基金涨跌",
                <Delta value={lot.exit_next_5d_return} />,
              ],
              ["离场后窗口终点", dateText(lot.post_window_end)],
            ]}
          />
          <p className="panel-note">
            离场后走势属于事后复盘。窗口行情不全或尚未赎回时显示「—」。
          </p>
        </div>
      )}
    </details>
  );
}
function PageRows({ rows, children }) {
  const [current, setCurrent] = useState(0);
  const size = 15,
    page = Math.min(current, Math.max(0, Math.ceil(rows.length / size) - 1));
  return (
    <>
      {children(rows.slice(page * size, (page + 1) * size))}
      {rows.length > size && (
        <div className="pager">
          <span>
            第 {page + 1} / {Math.ceil(rows.length / size)} 页 · 共{" "}
            {rows.length} 条
          </span>
          <button disabled={!page} onClick={() => setCurrent(page - 1)}>
            上一页
          </button>
          <button
            disabled={(page + 1) * size >= rows.length}
            onClick={() => setCurrent(page + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </>
  );
}
function Transactions({
  data,
  edition,
  disclosures,
  tab,
  setTab,
  selection,
  setSelection,
}) {
  const [filter, setFilter] = useState(""),
    [kind, setKind] = useState("");
  const lots = data.lots || [],
    orders = data.orders || [],
    isLots = tab === "lots";
  const funds = new Map((disclosures?.funds || []).map((f) => [f.code, f]));
  const choices = new Map(
    [...orders, ...lots].map((item) => [item.code, item.name || item.code]),
  );
  const items = (isLots ? lots : orders).filter(
    (row) =>
      (!filter || row.code === filter) &&
      (!kind || (isLots ? row.status : row.side) === kind) &&
      (selection == null ||
        (isLots ? [row.buy_order_id, row.sell_order_id] : [row.order_id]).some(
          (v) => v != null && String(v) === String(selection),
        )),
  );
  function jump(next, id) {
    setFilter("");
    setKind("");
    setSelection(id);
    setTab(next);
  }
  return (
    <Panel
      title={isLots ? "FIFO 配对批次" : "申赎成交"}
      extra={<small className="quiet">{items.length} 条记录</small>}
    >
      <div className="attr-controls">
        {selection != null && (
          <button className="control" onClick={() => setSelection(null)}>
            成交 #{selection} ×
          </button>
        )}
        <label>
          基金{" "}
          <select
            aria-label="按基金筛选"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">全部基金</option>
            {[...choices].map(([code, name]) => (
              <option key={code} value={code}>
                {code} · {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {isLots ? "批次状态" : "方向"}{" "}
          <select
            aria-label={isLots ? "批次状态" : "成交方向"}
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            <option value="">全部</option>
            {(isLots
              ? [
                  ["open", "持仓中"],
                  ["closed", "已平仓"],
                ]
              : [
                  ["buy", "申购"],
                  ["sell", "赎回"],
                ]
            ).map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {items.length === 0 ? (
        <Empty>当前筛选没有匹配的记录。</Empty>
      ) : (
        <PageRows key={`${filter}-${kind}-${selection}`} rows={items}>
          {(page) =>
            isLots ? (
              <div className="attr-lots">
                {page.map((l) => (
                  <Lot
                    key={l.lot_id}
                    lot={l}
                    edition={edition}
                    disclosure={funds.get(l.code)}
                    onOrder={(id) => jump("orders", id)}
                  />
                ))}
              </div>
            ) : (
              <Table
                headers={[
                  "编号 / 日期",
                  "基金",
                  "方向",
                  "份额",
                  "单位净值",
                  "金额（元）",
                  "费用（元）",
                  "已实现毛 / 净盈亏（元）",
                  "配对批次",
                ]}
                numeric={[3, 4, 5, 6]}
                rows={page.map((o) => [
                  <div className="cell-stack">
                    #{o.order_id}
                    <small>{dateText(o.date)}</small>
                  </div>,
                  <div className="cell-stack">
                    {o.name}
                    <small>{o.code}</small>
                  </div>,
                  label(o.side),
                  yuan(o.shares, 4),
                  num(o.nav, 4),
                  yuan(o.amount),
                  yuan(o.fee),
                  o.side === "sell"
                    ? `${yuan(o.realized_gross_pnl)} / ${yuan(o.realized_net_pnl)}`
                    : "—",
                  o.lot_ids?.length ? (
                    <button
                      className="replay-link"
                      onClick={() => jump("lots", o.order_id)}
                    >
                      查看 {o.lot_ids.length} 个配对
                    </button>
                  ) : (
                    "—"
                  ),
                ])}
              />
            )
          }
        </PageRows>
      )}
    </Panel>
  );
}
function Diagnostics({ items = [] }) {
  const [horizon, setHorizon] = useState(1),
    [group, setGroup] = useState("probability_bins");
  const row = items.find((o) => o.horizon_days === horizon),
    groups = row?.[group] || [];
  if (!items.length)
    return (
      <Panel title="预测诊断">
        <Empty>本版尚无可诊断的预测。</Empty>
      </Panel>
    );
  return (
    <Panel title="预测诊断">
      <div className="attr-controls">
        <label>
          期限{" "}
          <select
            value={horizon}
            aria-label="诊断预测期限"
            onChange={(e) => setHorizon(Number(e.target.value))}
          >
            <option value={1}>1个交易日</option>
            <option value={5}>5个交易日</option>
          </select>
        </label>
        <label>
          分组{" "}
          <select
            aria-label="诊断分组"
            value={group}
            onChange={(e) => setGroup(e.target.value)}
          >
            {[
              ["probability_bins", "预测概率区间"],
              ["by_direction", "预测方向"],
              ["by_samples", "历史样本数量"],
              ["by_momentum", "前5日动量"],
              ["by_strength", "发布时板块强度"],
              ["by_edge", "预测概率偏离五成的幅度"],
              ["by_board", "板块"],
            ].map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!row ? (
        <Empty>该期限尚无数据。</Empty>
      ) : (
        <>
          <Facts
            items={[
              ["已发布 / 已验证", `${row.total} / ${row.scored}`],
              ["命中 / 方向样本", `${row.hits} / ${row.directional}`],
              ["方向命中率", pctAbs(row.hit_rate, 2)],
              ["实际上涨率", pctAbs(row.up_rate, 2)],
              ["Brier 分数", num(row.brier, 4)],
              ["恒报50%的 Brier", num(row.baseline_brier, 4)],
              ["样本上涨频率对应 Brier", num(row.observed_base_rate_brier, 4)],
              [
                "动量反转 / 可比较样本",
                `${row.reversal_count ?? "—"} / ${row.reversal_sample ?? "—"}`,
              ],
              ["覆盖板块数", row.distinct_boards],
              ["预测数前三板块占比", pctAbs(row.top_three_board_share, 1)],
              ["待验证", row.pending],
              ["到期缺行情", row.overdue],
              ["期后补验", row.post_verified],
              ["最早输入日", dateText(row.first_input_date)],
              ["最晚输入日", dateText(row.last_input_date)],
              ["结果核验截至", dateText(row.score_through)],
            ]}
          />
          {groups.length ? (
            <Table
              maxHeight={440}
              headers={[
                "分组",
                "已验证 / 发布",
                "命中 / 方向样本",
                "命中率",
                "平均预测上涨概率",
                "实际上涨率",
                "Brier",
                "平均历史样本",
                "平均实际涨跌",
              ]}
              numeric={[1, 2, 3, 4, 5, 6, 7, 8]}
              rows={groups.map((r) => [
                r.label,
                `${r.scored} / ${r.total}`,
                `${r.hits} / ${r.directional}`,
                pctAbs(r.hit_rate, 2),
                pctAbs(r.mean_probability, 2),
                pctAbs(r.up_rate, 2),
                num(r.brier, 4),
                num(r.mean_samples, 1),
                pct(r.mean_return),
              ])}
            />
          ) : (
            <Empty>该分组无可用记录。</Empty>
          )}
          {row.method && <p className="panel-note">{row.method}</p>}
        </>
      )}
    </Panel>
  );
}
export default function Attribution({ edition, revision }) {
  const [tab, setTab] = useState("lots"),
    [selection, setSelection] = useState(null);
  const [tick, setTick] = useState(0);
  const result = useResource(
    `/api/agent/replay/attribution?version=${edition}`,
    `${revision}-${tick}`,
  );
  const account = useResource(`/api/agent/replay?version=${edition}`, revision);
  const data = result.data;
  useEffect(() => {
    if (!data || data.complete || result.loading) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), 20000);
    return () => window.clearTimeout(timer);
  }, [data, result.loading]);
  return (
    <div className="page-stack">
      <StateNotice resource={result} />
      {data &&
        (!data.summary ? (
          <Panel title="逐笔归因">
            <Empty>该版本尚无已归档的逐笔分析。</Empty>
          </Panel>
        ) : (
          <>
            <div className="replay-range">
              <strong>{edition === "v1" ? "原版" : "优化版"} · 逐笔归因</strong>
              <span>已核算至 {dateText(data.as_of)}</span>
              <Tag value={data.complete ? "ok" : "running"}>
                {data.complete ? "完整区间" : "回放中"}
              </Tag>
            </div>
            <div className="metrics">
              {[
                [
                  "账户净盈亏",
                  data.summary.net_pnl,
                  `期末资产 ${yuan(data.summary.ending_value)} 元`,
                ],
                [
                  "费前毛盈亏",
                  data.summary.gross_pnl,
                  `初始资产 ${yuan(data.summary.initial_capital)} 元`,
                ],
                [
                  "已实现净盈亏",
                  data.summary.realized_net_pnl,
                  `未实现净盈亏 ${yuan(data.summary.unrealized_net_pnl)} 元`,
                ],
                ["申购与赎回费用", data.summary.fees, "归集实际模拟流水费用"],
              ].map(([name, v, note]) => (
                <div key={name} className="metric">
                  <div className="metric-title">{name}（元）</div>
                  <div className="metric-number">{yuan(v)}</div>
                  <div className="metric-note">{note}</div>
                </div>
              ))}
            </div>
            <Sources
              summary={data.summary}
              audit={data.redemption_fee_audit}
              feeDocuments={data.redemption_fee_documents}
              onOrder={(id) => {
                setSelection(id);
                setTab("orders");
                requestAnimationFrame(() =>
                  document
                    .getElementById("attr-records")
                    ?.scrollIntoView({ block: "start" }),
                );
              }}
            />
            <Method lines={data.methodology} />
            <div className="desk-tabs" aria-label="归因明细" id="attr-records">
              {tabNames.map(([id, name]) => (
                <button
                  key={id}
                  aria-pressed={tab === id}
                  className={tab === id ? "selected" : ""}
                  onClick={() => {
                    setTab(id);
                    setSelection(null);
                  }}
                >
                  {name}
                </button>
              ))}
            </div>
            {tab === "forecasts" ? (
              <Diagnostics items={data.forecast_diagnostics} />
            ) : (
              <Transactions
                key={`${tab}-${selection ?? ""}`}
                data={data}
                edition={edition}
                disclosures={account.data?.fund_disclosures}
                tab={tab}
                setTab={setTab}
                selection={selection}
                setSelection={setSelection}
              />
            )}
          </>
        ))}
    </div>
  );
}
