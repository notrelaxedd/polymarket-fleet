// Drawing model helpers shared by the drawing layer, the sync queue and the tools rail (no DOM, no store).

import type { Drawing, DrawingInput, DrawingKind, DrawingPoint, DrawingScale, StickerKind } from "@shared/protocol";

/** activeTool ids the tools rail writes (see ToolsRail). */
export const DRAW_TOOLS: readonly DrawingKind[] = ["trend", "ray", "hline", "vline", "rect", "fib", "text", "measure"];
export const STICKER_KINDS: readonly StickerKind[] = ["target", "rocket", "warning", "check", "cross", "bull", "bear", "lightning"];

export interface ToolSpec {
  kind: DrawingKind;
  sticker: StickerKind | null;
}

/** "trend" -> {kind: trend}, "sticker:rocket" -> {kind: sticker, sticker: rocket}, "cursor"/unknown -> null */
export function parseTool(tool: string): ToolSpec | null {
  if (tool.startsWith("sticker:")) {
    const s = tool.slice(8) as StickerKind;
    return STICKER_KINDS.includes(s) ? { kind: "sticker", sticker: s } : null;
  }
  return (DRAW_TOOLS as readonly string[]).includes(tool) ? { kind: tool as DrawingKind, sticker: null } : null;
}

/** number of anchor points a kind is made of */
export function pointCount(kind: DrawingKind): 1 | 2 {
  return kind === "hline" || kind === "vline" || kind === "text" || kind === "sticker" ? 1 : 2;
}

/** Recolor swatches (SPEC token colors). The first is the default for line tools. */
export const PALETTE: readonly { color: string; label: string }[] = [
  { color: "#E6E9EF", label: "White" },
  { color: "#5B9CFF", label: "Blue" },
  { color: "#34D399", label: "Green" },
  { color: "#F87171", label: "Red" },
  { color: "#F5B942", label: "Amber" },
  { color: "#C29BFF", label: "Purple" },
];
export const DEFAULT_COLOR = PALETTE[0].color;

export const FIB_LEVELS: readonly number[] = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

export const TEXT_PLACEHOLDER = "Note";

export function defaultLineWidth(kind: DrawingKind): number {
  return kind === "hline" || kind === "vline" ? 1 : 1.5;
}

/** price rounding per scale: BTC to the cent, odds to 0.01¢ (clamped to 0–100) */
export function roundPrice(price: number, scale: DrawingScale): number {
  if (scale === "odds") return Math.round(Math.min(100, Math.max(0, price)) * 100) / 100;
  return Math.round(price * 100) / 100;
}

export function makePoint(time: number, price: number, scale: DrawingScale): DrawingPoint {
  return { time: Math.round(time), price: roundPrice(price, scale), scale };
}

let seq = 0;
/** client-generated drawing id (kept by the server on PUT /api/drawings) */
export function newDrawingId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return `d_${c.randomUUID().replace(/-/g, "").slice(0, 20)}`;
  seq = (seq + 1) % 1e6;
  return `d_${Date.now().toString(36)}${seq.toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export function newDrawing(kind: DrawingKind, points: DrawingPoint[], opts: { sticker?: StickerKind | null; now?: number } = {}): Drawing {
  const now = opts.now ?? Date.now();
  return {
    id: newDrawingId(),
    kind,
    points,
    color: DEFAULT_COLOR,
    locked: false,
    label: null,
    sticker: kind === "sticker" ? (opts.sticker ?? "target") : null,
    lineWidth: defaultLineWidth(kind),
    dashed: false,
    createdTs: now,
    updatedTs: now,
  };
}

/** the DrawingInput part of a drawing (what PUT /api/drawings/:id takes) */
export function inputOf(d: DrawingInput): DrawingInput {
  return {
    kind: d.kind,
    points: d.points.map((p) => ({ time: p.time, price: p.price, scale: p.scale })),
    color: d.color,
    locked: d.locked,
    label: d.label ?? null,
    sticker: d.sticker ?? null,
    lineWidth: d.lineWidth ?? defaultLineWidth(d.kind),
    dashed: d.dashed ?? false,
  };
}

/** same user-visible content (ignores ids and timestamps) */
export function sameInput(a: DrawingInput, b: DrawingInput): boolean {
  const x = inputOf(a);
  const y = inputOf(b);
  if (x.kind !== y.kind || x.color !== y.color || x.locked !== y.locked || x.label !== y.label) return false;
  if (x.sticker !== y.sticker || x.lineWidth !== y.lineWidth || x.dashed !== y.dashed) return false;
  if (x.points.length !== y.points.length) return false;
  return x.points.every((p, i) => p.time === y.points[i].time && p.price === y.points[i].price && p.scale === y.points[i].scale);
}

/** replace one drawing in a list (by id), bumping updatedTs */
export function replaceDrawing(list: readonly Drawing[], next: Drawing, now = Date.now()): Drawing[] {
  return list.map((d) => (d.id === next.id ? { ...next, updatedTs: now } : d));
}

const ALL_KINDS: readonly DrawingKind[] = [...DRAW_TOOLS, "sticker"];

/**
 * Shape validation for lists coming from the server / undo stacks: a known kind, a string id and color,
 * exactly the anchor points the kind needs (finite, one scale for all of them) and a known sticker.
 * The painter and the hit tester assume all of this, so anything else is dropped before it reaches them.
 */
export function isValidDrawing(d: unknown): d is Drawing {
  if (!d || typeof d !== "object") return false;
  const x = d as Partial<Drawing>;
  if (typeof x.id !== "string" || !x.id || typeof x.color !== "string") return false;
  if (!x.kind || !ALL_KINDS.includes(x.kind) || !Array.isArray(x.points)) return false;
  if (x.points.length !== pointCount(x.kind)) return false;
  const scale = x.points[0]?.scale;
  if (scale !== "btc" && scale !== "odds") return false;
  if (!x.points.every((p) => p && Number.isFinite(p.time) && Number.isFinite(p.price) && p.scale === scale)) return false;
  if (x.kind === "sticker" && x.sticker != null && !STICKER_KINDS.includes(x.sticker)) return false;
  if (x.lineWidth != null && !(Number.isFinite(x.lineWidth) && x.lineWidth > 0)) return false;
  return x.label == null || typeof x.label === "string";
}

/** the valid drawings of a list (the same array when nothing is dropped) */
export function validDrawings(list: readonly Drawing[]): readonly Drawing[] {
  return list.every((d) => isValidDrawing(d)) ? list : list.filter((d) => isValidDrawing(d));
}

/** "#RRGGBB" + alpha -> rgba() */
export function withAlpha(hex: string, a: number): string {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return hex;
  return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})`;
}

export const KIND_LABELS: Record<DrawingKind, string> = {
  trend: "Trend line",
  ray: "Ray",
  hline: "Horizontal line",
  vline: "Vertical line",
  rect: "Rectangle",
  fib: "Fib retracement",
  text: "Text note",
  measure: "Price range",
  sticker: "Sticker",
};
