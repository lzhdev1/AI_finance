import React from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { pctAbs } from "./format.js";

// ---------------------------------------------------------------- 指标

export function Stat({ label, value, color }) {
  return (
    <div className="stat">
      <div className="stat-k">{label}</div>
      <div className={`stat-v ${color || ""}`}>{value}</div>
    </div>
  );
}

export function Stats({ items }) {
  return (
    <div className="stats">
      {items.map((item) => (
        <Stat key={item.label} {...item} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- 成段文字

const NUMBER = /([+-]?\d+(?:\.\d+)?%?)/;

function decorate(text) {
  return String(text)
    .split(NUMBER)
    .map((part, index) =>
      NUMBER.test(part) && /^[+-]?\d/.test(part) ? (
        <b key={index} className="hl">{part}</b>
      ) : (
        <React.Fragment key={index}>{part}</React.Fragment>
      )
    );
}

// 按「；」「。」拆条；短句（如"东吴医疗服务股票C 15%"）并回上一条
export function splitClauses(text) {
  const raw = String(text || "").trim();
  if (!raw) return ["—"];
  const parts = raw.split(/[；;。]\s*/).map((item) => item.trim()).filter(Boolean);
  const merged = [];
  for (const part of parts) {
    const previous = merged[merged.length - 1];
    if (previous && part.length <= 20 && !part.includes("：")) {
      merged[merged.length - 1] = `${previous}；${part}`;
    } else {
      merged.push(part);
    }
  }
  return merged.length ? merged : [raw];
}

const TONES = {
  accent: "var(--accent)",
  warn: "#b54708",
  danger: "#b42318",
  ok: "#067647",
  neutral: "#6b7280",
};

export function Prose({ label, text, tone = "accent" }) {
  if (!text) return null;
  return (
    <div className="prose" style={{ "--tone": TONES[tone] || TONES.accent }}>
      <div className="prose-h">
        <span className="dot" />
        {label}
      </div>
      <ul>
        {splitClauses(text).map((item, index) => (
          <li key={index}>{decorate(item)}</li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- 表格

export function Table({ headers, rows, numeric = [], widths, maxHeight }) {
  if (!rows || rows.length === 0) return <p className="muted">暂无数据</p>;
  return (
    <div className="table-wrap" style={{ maxHeight }}>
      <table>
        {widths && (
          <colgroup>
            {widths.map((width, index) => (
              <col key={index} style={{ width }} />
            ))}
          </colgroup>
        )}
        <thead>
          <tr>
            {headers.map((header, index) => (
              <th key={index} className={numeric.includes(index) ? "num" : ""}>
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className={numeric.includes(cellIndex) ? "num" : ""}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- 概率条

export function Meter({ value }) {
  const width = Math.max(0, Math.min(100, Number(value || 0) * 100));
  return (
    <div className="meter-block">
      <div className="meter">
        <div className="meter-fill" style={{ width: `${width}%` }} />
        <div className="meter-tick" />
      </div>
      <div className="meter-cap">
        <span>0</span>
        <span>50% 抛硬币</span>
        <span>100</span>
      </div>
    </div>
  );
}

export function Chip({ children, kind = "neutral" }) {
  return <span className={`chip ${kind}`}>{children}</span>;
}

// ---------------------------------------------------------------- 曲线

export function Curve({ data, height = 300 }) {
  if (!data || data.length === 0) return <p className="muted">暂无净值记录</p>;
  const points = data.map((row) => ({
    date: String(row.trade_date || "").slice(5, 10),
    pod: Number(row.cum_return),
    bench: row.benchmark_return === null ? null : Number(row.benchmark_return),
  }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
        <CartesianGrid stroke="rgba(22,24,29,.06)" vertical={false} />
        <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#6b7280" }} axisLine={false} tickLine={false} />
        <YAxis
          tick={{ fontSize: 11, fill: "#6b7280" }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(value) => `${(value * 100).toFixed(0)}%`}
        />
        <Tooltip
          formatter={(value, name) => [pctAbs(value), name === "pod" ? "本仓" : "沪深300"]}
          labelStyle={{ fontSize: 12 }}
          contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid rgba(22,24,29,.1)" }}
        />
        <Legend
          verticalAlign="top"
          align="left"
          height={28}
          formatter={(value) => (value === "pod" ? "本仓" : "沪深300")}
          wrapperStyle={{ fontSize: 12 }}
        />
        <Line type="monotone" dataKey="bench" stroke="#b54708" strokeWidth={1.3} dot={false} strokeDasharray="3 3" />
        <Line type="monotone" dataKey="pod" stroke="#1f4e79" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function SectionTitle({ title, sub, badge }) {
  return (
    <div className="section-title">
      <span className="bar" />
      <span className="title">{title}</span>
      {badge && <Chip kind="ok">{badge}</Chip>}
      {sub && <span className="sub">{sub}</span>}
    </div>
  );
}