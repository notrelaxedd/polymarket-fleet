// Pure geometry + data shaping for the Pair relationship cards (plain SVG, no chart library). OWNER: tabs.
// Everything here is DOM-free so it can be unit tested (src/components/tabs/__tests__/pair-geom.test.ts).

import type { LagBar, ScatterPoint, SensitivityPoint } from "@shared/protocol";

export const WINDOW_SEC = 15 * 60;

/** Card colors (SPEC token table). */
export const PAIR_COLORS = {
  odds: "#5B9CFF",
  oddsText: "#7FB2FF",
  fair: "#C29BFF",
  ptb: "#F5B942",
  grid: "#1E2530",
  gridStrong: "#2A3442",
  axis: "#8A93A3",
  shade: "#151B24",
  lagBar: "#3D5F94",
} as const;

/** Time-left buckets for scatter points, earliest (most time left) first: dark slate -> light blue. */
export const TLEFT_BUCKETS: { minSec: number; label: string; color: string }[] = [
  { minSec: 600, label: ">10m", color: "#3E4C63" },
  { minSec: 300, label: "5–10m", color: "#4E6E9E" },
  { minSec: 120, label: "2–5m", color: "#6C9AE0" },
  { minSec: -Infinity, label: "<2m", color: "#B4D3FF" },
];

/** Index into TLEFT_BUCKETS for a sample with `tLeftSec` seconds left. */
export function tLeftBucket(tLeftSec: number): number {
  for (let i = 0; i < TLEFT_BUCKETS.length; i++) if (tLeftSec >= TLEFT_BUCKETS[i].minSec) return i;
  return TLEFT_BUCKETS.length - 1;
}

/** Linear map from [d0, d1] to [r0, r1] (a zero-width domain maps to the middle of the range). */
export function linear(d0: number, d1: number, r0: number, r1: number): (v: number) => number {
  const span = d1 - d0;
  if (span === 0 || !Number.isFinite(span)) return () => (r0 + r1) / 2;
  const k = (r1 - r0) / span;
  return (v: number) => r0 + (v - d0) * k;
}

/** "Nice" tick values covering [min, max] with roughly `count` steps (1/2/5 x 10^n). */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  if (min > max) [min, max] = [max, min];
  const raw = (max - min) / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  const start = Math.ceil(min / step - 1e-9) * step;
  for (let v = start; v <= max + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : +v.toFixed(10));
  return out;
}

/**
 * A single path of zero-length segments, one per dot ("M x y h0"); drawn with a round line cap this renders
 * thousands of scatter points as ONE SVG element (cheap to paint and diff).
 */
export function dotsPath(pts: readonly { x: number; y: number }[]): string {
  let d = "";
  for (const p of pts) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    d += `M${p.x.toFixed(1)} ${p.y.toFixed(1)}h0`;
  }
  return d;
}

/** Polyline path through [x, y] points; a null / non-finite y breaks the line (gap). */
export function linePath(pts: readonly (readonly [number, number | null])[]): string {
  let d = "";
  let pen = false;
  for (const [x, y] of pts) {
    if (y == null || !Number.isFinite(y) || !Number.isFinite(x)) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    pen = true;
  }
  return d;
}

/**
 * At most `max` items, evenly strided over the array, always keeping the last one (the newest sample is the
 * most relevant). Order is preserved.
 */
export function sampleEvenly<T>(arr: readonly T[], max: number): T[] {
  if (max <= 0) return [];
  if (arr.length <= max) return arr.slice();
  const out: T[] = [];
  const step = (arr.length - 1) / (max - 1 || 1);
  for (let i = 0; i < max; i++) out.push(arr[Math.round(i * step)]);
  return out;
}

/** Quantile (0..1) of finite values; null when there are none. */
export function quantile(values: readonly number[], q: number): number | null {
  const v = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
  if (!v.length) return null;
  const pos = Math.min(v.length - 1, Math.max(0, q * (v.length - 1)));
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return v[lo] + (v[hi] - v[lo]) * (pos - lo);
}

// ---------------------------------------------------------------------------------------------------------
// Scatter

export const Z_MIN = -2;
export const Z_MAX = 2;

export interface ScatterLayer {
  bucket: number;
  color: string;
  label: string;
  pts: { x: number; y: number }[];
}

/**
 * Bucket scatter points by time left (in TLEFT_BUCKETS order, so later-in-window colors paint on top),
 * dropping points outside ±2σ and down-sampling to `maxPoints` in total.
 */
export function scatterLayers(
  points: readonly ScatterPoint[],
  x: (z: number) => number,
  y: (c: number) => number,
  maxPoints = 2400,
): { layers: ScatterLayer[]; shown: number; offScale: number } {
  const inRange = points.filter((p) => Number.isFinite(p.z) && Number.isFinite(p.upMid) && p.z >= Z_MIN && p.z <= Z_MAX);
  const sampled = sampleEvenly(inRange, maxPoints);
  const layers: ScatterLayer[] = TLEFT_BUCKETS.map((b, bucket) => ({ bucket, color: b.color, label: b.label, pts: [] }));
  for (const p of sampled) layers[tLeftBucket(p.tLeftSec)].pts.push({ x: x(p.z), y: y(Math.min(100, Math.max(0, p.upMid))) });
  return { layers, shown: sampled.length, offScale: points.length - inRange.length };
}

// ---------------------------------------------------------------------------------------------------------
// Lag

/** Symmetric-ish ρ domain that always contains 0 and leaves headroom for the peak label. */
export function lagDomain(bars: readonly LagBar[]): [number, number] {
  const rs = bars.map((b) => b.rho).filter((r): r is number => r != null && Number.isFinite(r));
  const hi = Math.max(0.2, ...rs.map((r) => r));
  const lo = Math.min(0, ...rs);
  return [lo < 0 ? Math.max(-1, lo * 1.15) : 0, Math.min(1, hi * 1.3)];
}

/** "4s · ρ 0.61" */
export function lagPeakLabel(peak: { lagSec: number; rho: number } | null): string | null {
  if (!peak || !Number.isFinite(peak.rho)) return null;
  const r = peak.rho;
  return `${peak.lagSec}s · ρ ${r < 0 ? "−" : ""}${Math.abs(r).toFixed(2)}`;
}

/**
 * Peak bar: the payload's `lagPeak` when it matches a bar, else the bar with the highest ρ (null when no bar
 * has data).
 */
export function resolveLagPeak(bars: readonly LagBar[], peak: { lagSec: number; rho: number } | null): { lagSec: number; rho: number } | null {
  if (peak && bars.some((b) => b.lagSec === peak.lagSec)) return peak;
  let best: { lagSec: number; rho: number } | null = null;
  for (const b of bars) if (b.rho != null && Number.isFinite(b.rho) && (!best || b.rho > best.rho)) best = { lagSec: b.lagSec, rho: b.rho };
  return best;
}

// ---------------------------------------------------------------------------------------------------------
// Sensitivity

/** Window bounds implied by sensitivity samples (ts + tLeft = window end). Null when empty. */
export function sensitivityWindow(points: readonly SensitivityPoint[]): { startTs: number; endTs: number } | null {
  const last = points[points.length - 1];
  if (!last) return null;
  const endTs = Math.round((last.ts + last.tLeftSec * 1000) / 1000) * 1000;
  return { startTs: endTs - WINDOW_SEC * 1000, endTs };
}

/**
 * y domain for sensitivity lines (¢ per $10). Near expiry next to the strike sensitivity explodes, so the top
 * uses the 97th percentile (with headroom) and the lines are clipped; the bottom includes 0.
 */
export function sensitivityDomain(points: readonly SensitivityPoint[]): [number, number] {
  const vals: number[] = [];
  for (const p of points) {
    if (p.market != null && Number.isFinite(p.market)) vals.push(p.market);
    if (p.fair != null && Number.isFinite(p.fair)) vals.push(p.fair);
  }
  if (!vals.length) return [0, 1];
  const hi = quantile(vals, 0.97) ?? 1;
  const lo = quantile(vals, 0.03) ?? 0;
  const top = Math.max(0.5, hi * 1.2);
  const bottom = Math.min(0, lo < 0 ? lo * 1.2 : 0);
  return [bottom, top];
}

/** Last finite value of a field (the "now" label). */
export function lastValue(points: readonly SensitivityPoint[], key: "market" | "fair"): { ts: number; v: number } | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const v = points[i][key];
    if (v != null && Number.isFinite(v)) return { ts: points[i].ts, v };
  }
  return null;
}

/**
 * Vertical positions for two end-of-line labels so they never overlap: keeps each near its own y and pushes
 * them apart by at least `gap` px, clamped to [top, bottom].
 */
export function separateLabels(a: number | null, b: number | null, gap: number, top: number, bottom: number): [number | null, number | null] {
  const clamp = (v: number) => Math.min(bottom, Math.max(top, v));
  if (a == null || b == null) return [a == null ? null : clamp(a), b == null ? null : clamp(b)];
  let ya = clamp(a);
  let yb = clamp(b);
  if (Math.abs(ya - yb) >= gap) return [ya, yb];
  const mid = (ya + yb) / 2;
  const aAbove = ya <= yb;
  ya = mid + (aAbove ? -gap / 2 : gap / 2);
  yb = mid + (aAbove ? gap / 2 : -gap / 2);
  // shift the pair back inside the plot
  const lo = Math.min(ya, yb);
  const hi = Math.max(ya, yb);
  const shift = lo < top ? top - lo : hi > bottom ? bottom - hi : 0;
  return [ya + shift, yb + shift];
}
