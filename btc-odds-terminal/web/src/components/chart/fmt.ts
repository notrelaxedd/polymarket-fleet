// Number formatting used on canvas + tooltip (chart-local so the chart has no UI-layer dependencies).

const usd2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** 67012.4 -> "67,012.40" */
export const fmtBtc = (v: number) => usd2.format(v);

/** signed USD delta: "+43.20" / "−12.05" */
export const fmtSignedUsd = (v: number) => `${v >= 0 ? "+" : "−"}${usd2.format(Math.abs(v))}`;

/** cents with one decimal unless integral: 62 -> "62¢", 62.5 -> "62.5¢" */
export function fmtCents(v: number): string {
  const r = Math.round(v * 10) / 10;
  return `${Number.isInteger(r) ? r.toFixed(0) : r.toFixed(1)}¢`;
}

/** signed cents: "+2.4¢" / "−1.0¢" */
export const fmtSignedCents = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}¢`;
