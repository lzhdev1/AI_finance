// 展示口径集中在这一点：红涨绿跌、单位、缺值一律 "—"。
export const isNil = (value) => value === null || value === undefined || Number.isNaN(value);

export function forecastDirection(probability) {
  if (isNil(probability)) return "—";
  return probability > 0.5 ? "看涨" : probability < 0.5 ? "看跌" : "中性";
}

export function cls(value) {
  if (isNil(value) || Math.abs(Number(value)) < 1e-9) return "flat";
  return Number(value) > 0 ? "up" : "down";
}

// 库里以"小数"存的比例（0.0123 = 1.23%）
export function pct(value, digits = 2) {
  if (isNil(value)) return "—";
  const number = Number(value) * 100;
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

export function pctAbs(value, digits = 2) {
  if (isNil(value)) return "—";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

// 库里以"百分点"存的数值（-0.406 表示 -0.406%）
export function pts(value, digits = 2) {
  if (isNil(value)) return "—";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

export function ratio(value, digits = 2) {
  if (isNil(value)) return "—";
  return `${Number(value).toFixed(digits)}x`;
}

export function money(value) {
  if (isNil(value)) return "—";
  const number = Number(value);
  return Math.abs(number) >= 10000 ? `${(number / 10000).toFixed(2)} 万` : `${number.toFixed(0)} 元`;
}

export function num(value, digits = 2) {
  if (isNil(value)) return "—";
  return Number(value).toFixed(digits);
}

export function streakText(days) {
  if (!days) return "—";
  return days > 0 ? `连涨 ${days} 天` : `连跌 ${Math.abs(days)} 天`;
}

export function shortDate(iso) {
  if (!iso) return "—";
  return String(iso).slice(5, 10);
}
