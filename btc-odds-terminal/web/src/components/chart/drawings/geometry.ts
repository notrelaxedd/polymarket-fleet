// Pure geometry for chart drawings: screen-space hit testing, ray clipping, drag math in data space,
// fib levels, price-range stats and text wrapping. No canvas, no DOM, no store (unit-tested).

import type { Drawing, DrawingPoint } from "@shared/protocol";
import { FIB_LEVELS, roundPrice } from "./model";

export interface XY {
  x: number;
  y: number;
}
export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}
export interface View {
  width: number;
  height: number;
}

/** p0/p1 = 2-point anchors, c0..c3 = rectangle corners (c0 = p0, c2 = p1), body = the whole drawing */
export type Part = "body" | "p0" | "p1" | "c0" | "c1" | "c2" | "c3";

/** A drawing projected to pane (media px) coordinates. */
export interface Projected {
  d: Drawing;
  /** anchor points in screen space (may lie outside the pane) */
  pts: XY[];
  /** text card / sticker badge / horizontal-line label pill */
  box?: Box | null;
  /** fib: screen y of each level (as painted; linear between the anchors when absent) */
  levelYs?: number[];
}

export interface Hit {
  id: string;
  part: Part;
  dist: number;
  /**
   * The press landed inside a filled area (rectangle / fib / measure box), not on a stroke, label or
   * handle. Strokes of every drawing win over fills; an unselected drawing's fill only selects on click
   * and never takes the pan / drag away from the chart.
   */
  fill?: boolean;
}

export const HANDLE_R = 5;

export function distToSegment(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  let t = len2 > 0 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

const inBox = (p: XY, b: Box, pad = 0) => p.x >= b.left - pad && p.x <= b.right + pad && p.y >= b.top - pad && p.y <= b.bottom + pad;

/** distance from p to the outline of box b */
function distToBorder(p: XY, b: Box): number {
  const tl = { x: b.left, y: b.top };
  const tr = { x: b.right, y: b.top };
  const br = { x: b.right, y: b.bottom };
  const bl = { x: b.left, y: b.bottom };
  return Math.min(distToSegment(p, tl, tr), distToSegment(p, tr, br), distToSegment(p, br, bl), distToSegment(p, bl, tl));
}

/**
 * Far end of a ray from `a` through `b`, clipped to the pane grown by `margin` on every side.
 * Returns `b` when the two points coincide.
 */
export function rayEnd(a: XY, b: XY, view: View, margin = 50): XY {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) return b;
  const xmin = -margin;
  const xmax = view.width + margin;
  const ymin = -margin;
  const ymax = view.height + margin;
  let t = Infinity;
  if (dx > 0) t = Math.min(t, (xmax - a.x) / dx);
  else if (dx < 0) t = Math.min(t, (xmin - a.x) / dx);
  if (dy > 0) t = Math.min(t, (ymax - a.y) / dy);
  else if (dy < 0) t = Math.min(t, (ymin - a.y) / dy);
  // the anchor itself is outside the grown pane and the ray points away: draw nothing past b
  if (!Number.isFinite(t) || t < 1) return b;
  return { x: a.x + dx * t, y: a.y + dy * t };
}

export function normBox(a: XY, b: XY): Box {
  return { left: Math.min(a.x, b.x), top: Math.min(a.y, b.y), right: Math.max(a.x, b.x), bottom: Math.max(a.y, b.y) };
}

/** rectangle corners c0..c3 in screen space: c0 = p0, c1 = (p1.x, p0.y), c2 = p1, c3 = (p0.x, p1.y) */
export function rectCorners(p0: XY, p1: XY): XY[] {
  return [p0, { x: p1.x, y: p0.y }, p1, { x: p0.x, y: p1.y }];
}

/** draggable handles of a (selected) drawing */
export function handlesOf(sh: Projected, view: View): { part: Part; at: XY }[] {
  const [a, b] = sh.pts;
  switch (sh.d.kind) {
    case "trend":
    case "ray":
    case "fib":
    case "measure":
      return b ? [{ part: "p0", at: a }, { part: "p1", at: b }] : [];
    case "rect":
      return b ? rectCorners(a, b).map((at, i) => ({ part: `c${i}` as Part, at })) : [];
    case "hline":
      return [{ part: "body", at: { x: view.width / 2, y: a.y } }];
    case "vline":
      return [{ part: "body", at: { x: a.x, y: view.height / 2 } }];
    default:
      return [];
  }
}

/** y of each fib level between p0 (level 1) and p1 (level 0) */
export function fibLevels(p0Price: number, p1Price: number): { level: number; price: number }[] {
  return FIB_LEVELS.map((level) => ({ level, price: p1Price + (p0Price - p1Price) * level }));
}

/** fib level ys in screen space (linear between the two anchors on one price scale) */
function fibLevelYs(a: XY, b: XY): number[] {
  return FIB_LEVELS.map((level) => b.y + (a.y - b.y) * level);
}

export type HitMode = "stroke" | "fill" | "any";

/**
 * Hit-test one projected drawing at `p`. `tol` is the stroke grab distance in px (larger for touch).
 * Handles of the selected drawing win over its body. `mode` limits the test to strokes / labels / handles
 * ("stroke") or to filled interiors ("fill"); "any" tries strokes first.
 */
export function hitShape(sh: Projected, p: XY, tol: number, view: View, selected: boolean, mode: HitMode = "any"): Hit | null {
  if (mode === "any") return hitShape(sh, p, tol, view, selected, "stroke") ?? hitShape(sh, p, tol, view, selected, "fill");
  const id = sh.d.id;
  const [a, b] = sh.pts;
  if (!a) return null;
  if (mode === "fill") {
    if (!b || (sh.d.kind !== "rect" && sh.d.kind !== "measure" && sh.d.kind !== "fib")) return null;
    return inBox(p, normBox(a, b)) ? { id, part: "body", dist: tol, fill: true } : null;
  }
  if (selected) {
    let best: Hit | null = null;
    for (const h of handlesOf(sh, view)) {
      const dist = Math.hypot(p.x - h.at.x, p.y - h.at.y);
      if (dist <= tol + HANDLE_R && (!best || dist < best.dist)) best = { id, part: h.part, dist };
    }
    if (best) return best;
  }
  const body = (dist: number): Hit | null => (dist <= tol ? { id, part: "body", dist } : null);
  switch (sh.d.kind) {
    case "trend":
      return b ? body(distToSegment(p, a, b)) : null;
    case "ray":
      return b ? body(distToSegment(p, a, rayEnd(a, b, view))) : null;
    case "hline":
      if (sh.box && inBox(p, sh.box, 2)) return { id, part: "body", dist: 0 };
      return body(Math.abs(p.y - a.y));
    case "vline":
      return body(Math.abs(p.x - a.x));
    case "rect":
      return b ? body(distToBorder(p, normBox(a, b))) : null;
    case "measure": {
      if (!b) return null;
      if (sh.box && inBox(p, sh.box, 2)) return { id, part: "body", dist: 0 };
      const bx = normBox(a, b);
      const cx = (bx.left + bx.right) / 2;
      const cy = (bx.top + bx.bottom) / 2;
      // outline + the two arrows through the middle
      return body(
        Math.min(distToBorder(p, bx), distToSegment(p, { x: cx, y: bx.top }, { x: cx, y: bx.bottom }), distToSegment(p, { x: bx.left, y: cy }, { x: bx.right, y: cy })),
      );
    }
    case "fib": {
      if (!b) return null;
      const left = Math.min(a.x, b.x);
      const right = Math.max(a.x, b.x);
      let dist = distToSegment(p, a, b);
      for (const y of sh.levelYs ?? fibLevelYs(a, b)) dist = Math.min(dist, distToSegment(p, { x: left, y }, { x: right, y }));
      return body(dist);
    }
    case "text":
    case "sticker":
      return sh.box && inBox(p, sh.box, sh.d.kind === "sticker" ? 2 : 0) ? { id, part: "body", dist: 0 } : null;
  }
  return null;
}

/**
 * Topmost hit (drawings later in the list are drawn on top). Strokes, labels and handles of every drawing
 * come first — the selected drawing's before the rest — and filled interiors only after them, so a line
 * inside a rectangle stays grabbable.
 */
export function hitTestAll(shapes: readonly Projected[], p: XY, tol: number, view: View, selectedId: string | null): Hit | null {
  const sel = selectedId ? shapes.find((s) => s.d.id === selectedId) : undefined;
  for (const mode of ["stroke", "fill"] as const) {
    if (sel) {
      const h = hitShape(sel, p, tol, view, true, mode);
      if (h) return h;
    }
    for (let i = shapes.length - 1; i >= 0; i--) {
      const s = shapes[i];
      if (s === sel) continue;
      const h = hitShape(s, p, tol, view, false, mode);
      if (h) return h;
    }
  }
  return null;
}

/** screen bounding box of a projected drawing, clipped to the pane (null when entirely off-screen) */
export function shapeBox(sh: Projected, view: View): Box | null {
  const [a, b] = sh.pts;
  if (!a) return null;
  let bx: Box;
  switch (sh.d.kind) {
    case "hline":
      bx = sh.box ? { left: sh.box.left, top: Math.min(a.y, sh.box.top), right: sh.box.right, bottom: Math.max(a.y, sh.box.bottom) } : { left: 0, top: a.y, right: view.width, bottom: a.y };
      break;
    case "vline":
      bx = { left: a.x, top: view.height * 0.25, right: a.x, bottom: view.height * 0.25 };
      break;
    case "text":
    case "sticker":
      bx = sh.box ?? { left: a.x, top: a.y, right: a.x, bottom: a.y };
      break;
    case "ray":
      bx = b ? normBox(a, rayEnd(a, b, view, 0)) : normBox(a, a);
      break;
    default:
      bx = b ? normBox(a, b) : normBox(a, a);
  }
  if (bx.right < 0 || bx.left > view.width || bx.bottom < 0 || bx.top > view.height) return null;
  return {
    left: Math.max(0, bx.left),
    top: Math.max(0, bx.top),
    right: Math.min(view.width, bx.right),
    bottom: Math.min(view.height, bx.bottom),
  };
}

// ---- drag math (data space) -------------------------------------------------------------------------

const movePoint = (p: DrawingPoint, dt: number, dp: number): DrawingPoint => ({
  time: Math.round(p.time + dt),
  price: roundPrice(p.price + dp, p.scale),
  scale: p.scale,
});

/** a point a drawing may be saved with: finite time, finite price, BTC above zero (odds are clamped 0–100) */
export function pointInRange(p: DrawingPoint): boolean {
  return Number.isFinite(p.time) && p.time > 0 && Number.isFinite(p.price) && (p.scale === "odds" || p.price > 0);
}

/**
 * Translate the whole drawing by dt ms / dp price units (hline: price only, vline: time only).
 * A move that would leave any point out of range is refused (the drawing is returned unchanged).
 */
export function moveBody(d: Drawing, dt: number, dp: number): Drawing {
  if (!Number.isFinite(dt) || !Number.isFinite(dp)) return d;
  const t = d.kind === "hline" ? 0 : dt;
  const v = d.kind === "vline" ? 0 : dp;
  const points = d.points.map((p) => movePoint(p, t, v));
  return points.every(pointInRange) ? { ...d, points } : d;
}

/**
 * Move one handle to (time, price). `level` keeps a trend / ray / measure horizontal (Shift).
 * Rectangle corners move the two coordinates they own.
 */
export function moveHandle(d: Drawing, part: Part, time: number, price: number, opts: { level?: boolean } = {}): Drawing {
  if (part === "body") return d;
  const pts = d.points.map((p) => ({ ...p }));
  const [p0, p1] = pts;
  if (!p0 || !p1) return d;
  const t = Math.round(time);
  const set = (p: DrawingPoint, tt: number | null, pp: number | null) => {
    if (tt != null) p.time = tt;
    if (pp != null) p.price = roundPrice(pp, p.scale);
  };
  switch (part) {
    case "p0":
    case "p1": {
      const me = part === "p0" ? p0 : p1;
      const other = part === "p0" ? p1 : p0;
      const level = opts.level && (d.kind === "trend" || d.kind === "ray" || d.kind === "measure");
      set(me, t, level ? other.price : price);
      break;
    }
    case "c0":
      set(p0, t, price);
      break;
    case "c1":
      set(p1, t, null);
      set(p0, null, price);
      break;
    case "c2":
      set(p1, t, price);
      break;
    case "c3":
      set(p0, t, null);
      set(p1, null, price);
      break;
  }
  return { ...d, points: pts };
}

// ---- price-range measure ----------------------------------------------------------------------------

export interface MeasureStats {
  /** p1 − p0 in the scale's units */
  delta: number;
  /** percent change (BTC scale only) */
  pct: number | null;
  bars: number;
  durationMs: number;
  up: boolean;
}

export function measureStats(p0: DrawingPoint, p1: DrawingPoint, tfSec: number): MeasureStats {
  const delta = p1.price - p0.price;
  const durationMs = Math.abs(p1.time - p0.time);
  return {
    delta,
    pct: p0.scale === "btc" && p0.price !== 0 ? (delta / p0.price) * 100 : null,
    bars: Math.round(durationMs / (Math.max(1, tfSec) * 1000)),
    durationMs,
    up: delta >= 0,
  };
}

/** 45s · 3m 15s · 1h 2m */
export function fmtDuration(ms: number): string {
  const s = Math.round(Math.abs(ms) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
}

// ---- text -------------------------------------------------------------------------------------------

/** Greedy word wrap; words wider than maxW are broken by characters. Keeps explicit line breaks. */
export function wrapText(text: string, maxW: number, measure: (s: string) => number, maxLines = 12): string[] {
  const out: string[] = [];
  let truncated = false;
  const push = (s: string) => {
    if (out.length < maxLines) out.push(s);
    else truncated = true;
  };
  for (const para of text.split(/\r?\n/)) {
    const words = para.split(/\s+/).filter(Boolean);
    if (!words.length) {
      push("");
      continue;
    }
    let line = "";
    for (let w of words) {
      const cand = line ? `${line} ${w}` : w;
      if (measure(cand) <= maxW) {
        line = cand;
        continue;
      }
      if (line) push(line);
      line = "";
      while (measure(w) > maxW && w.length > 1) {
        let n = w.length - 1;
        while (n > 1 && measure(w.slice(0, n)) > maxW) n--;
        push(w.slice(0, n));
        w = w.slice(n);
      }
      line = w;
    }
    push(line);
  }
  if (truncated && out.length) out[out.length - 1] = `${out[out.length - 1]}…`;
  return out;
}
