// Pure model for the chart's position lines (no DOM, no canvas): which lines exist, where they sit on the
// odds (left, 0–100¢) scale, what their labels say, how far a TP/SL leg may be dragged, and how labels
// stack when lines are close together. PositionLinesPrimitive draws it; LabelOverlay renders the labels.
//
// Prices are the POSITION SIDE's cents: an Up position sits at Up prices, a Down position at Down prices
// (the same values the "Down odds" overlay plots), so the axis, the label and the bracket all agree. The axis
// tag of a Down line says so ("D 47¢"): the left scale is otherwise read against the Up odds line.
//
// A position's TP/SL can only be moved or added while there is a FRESH bid for its side (`LinesInput.bids`):
// the range keeps a TP above / a SL below the bid so a leg never fires the moment it is saved, and without a
// current quote there is nothing to measure that against.

import type { Bracket, BracketInput, Fill, Position, Side } from "@shared/protocol";
import { MAX_PRICE_CENTS, MIN_PRICE_CENTS, pnlUsd } from "@shared/paper";

export type LegKind = "tp" | "sl";
export type LineKind = "entry" | LegKind;

/** A label is a row of colored text runs (rendered in the overlay, mono font). */
export type RunTone = "side-up" | "side-down" | "entry" | "tp" | "sl" | "muted" | "pos" | "neg" | "flat";
export interface LabelRun {
  text: string;
  tone: RunTone;
}

export interface FillMarker {
  ts: number;
  cents: number;
}

export interface PositionLine {
  /** stable key: `${positionId}:${kind}` or `draft:${kind}` */
  key: string;
  kind: LineKind;
  /** ticket preview (before entry) rather than an open position */
  draft: boolean;
  positionId: string | null;
  side: Side;
  /** price of the line, the side's cents */
  cents: number;
  runs: LabelRun[];
  /** accessible description of the line ("Take profit 96¢, projected +$67.33") */
  aria: string;
  /** TP/SL: the drag range in whole cents (null = cannot be moved right now) */
  bounds: Bounds | null;
  /** entry: buy fills to mark with a triangle */
  markers: FillMarker[];
  /** entry of a position: offer "+TP" / "+SL" chips for legs that are off */
  addTp: boolean;
  addSl: boolean;
}

export interface Bounds {
  lo: number;
  hi: number;
}

// ---- formatting ---------------------------------------------------------------------------------------

/** price at 0.1¢ resolution without the sign: 26 -> "26", 26.44 -> "26.4" */
export function fmtPrice(c: number): string {
  const r = Math.round(c * 10) / 10;
  return Number.isInteger(r) ? r.toFixed(0) : r.toFixed(1);
}

/** contracts at 0.1 resolution: 96.2 -> "96.2", 100 -> "100" */
export const fmtQty = fmtPrice;

/** "+$59.64" / "−$3.20" / "$0.00" (null -> "—") */
export function fmtPnl(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const r = Math.round(v * 100) / 100;
  const abs = Math.abs(r).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (r === 0) return `$${abs}`;
  return `${r > 0 ? "+" : "−"}$${abs}`;
}

const pnlTone = (v: number | null): RunTone => (v == null || Math.round(v * 100) === 0 ? "flat" : v > 0 ? "pos" : "neg");

// ---- drag bounds --------------------------------------------------------------------------------------

export interface LegContext {
  /** the other legs of the bracket (side cents), null = off */
  tpCents: number | null;
  slCents: number | null;
  /** current bid of the side: a TP at/below it or a SL at/above it would exit at once */
  bid: number | null;
  /** entry price (ticket preview only): TP must stay above / SL below it */
  entry: number | null;
}

/**
 * Whole-cent range a leg may be dragged to. Mirrors the ticket / Edit bracket validation:
 * TP > bid, TP > entry (preview), TP > SL; SL < bid, SL < entry (preview), SL < TP; 1–99¢.
 * Null when the range is empty (e.g. the bid is 99¢ and there is no room for a TP).
 */
export function legBounds(kind: LegKind, c: LegContext): Bounds | null {
  let lo = MIN_PRICE_CENTS;
  let hi = MAX_PRICE_CENTS;
  const above = (v: number | null) => {
    if (v != null && Number.isFinite(v)) lo = Math.max(lo, Math.floor(v + 1e-9) + 1);
  };
  const below = (v: number | null) => {
    if (v != null && Number.isFinite(v)) hi = Math.min(hi, Math.ceil(v - 1e-9) - 1);
  };
  if (kind === "tp") {
    above(c.bid);
    above(c.entry);
    above(c.slCents);
  } else {
    below(c.bid);
    below(c.entry);
    below(c.tpCents);
  }
  return lo <= hi ? { lo, hi } : null;
}

/** Snap a raw scale value to whole cents inside `b` (null when the leg cannot move). */
export function snapCents(raw: number | null, b: Bounds | null): number | null {
  if (raw == null || !Number.isFinite(raw) || !b) return null;
  return Math.min(b.hi, Math.max(b.lo, Math.round(raw)));
}

/** Nudge by `step` cents from `cur`, clamped to `b`. */
export function nudgeCents(cur: number, step: number, b: Bounds | null): number | null {
  if (!b) return null;
  return Math.min(b.hi, Math.max(b.lo, Math.round(cur) + step));
}

// ---- bracket helpers ----------------------------------------------------------------------------------

export function bracketInput(b: BracketInput | null | undefined): BracketInput {
  return {
    tpCents: b?.tpCents ?? null,
    slCents: b?.slCents ?? null,
    btcStop: b?.btcStop ?? null,
    timeExitSec: b?.timeExitSec ?? null,
    maxSlippageCents: b?.maxSlippageCents ?? null,
  };
}

/** `base` with one leg replaced (null = leg off); other legs and the slippage cap are kept. */
export function withLeg(base: BracketInput | null | undefined, kind: LegKind, cents: number | null): BracketInput {
  const b = bracketInput(base);
  if (kind === "tp") b.tpCents = cents;
  else b.slCents = cents;
  return b;
}

export function sameBracket(a: BracketInput | null | undefined, b: BracketInput | null | undefined): boolean {
  const x = bracketInput(a);
  const y = bracketInput(b);
  return (
    x.tpCents === y.tpCents &&
    x.slCents === y.slCents &&
    x.btcStop === y.btcStop &&
    x.timeExitSec === y.timeExitSec &&
    x.maxSlippageCents === y.maxSlippageCents
  );
}

/**
 * Drag range of a position's leg: like legBounds, against the fresh client bid and the server's mark
 * (whichever is closer to the leg's side of the market). Null without a fresh bid (no quote / stale feed).
 */
export function positionLegBounds(
  kind: LegKind,
  legs: { tpCents: number | null; slCents: number | null },
  bid: number | null,
  mark: number | null,
): Bounds | null {
  if (bid == null || !Number.isFinite(bid)) return null;
  const m = mark != null && Number.isFinite(mark) ? mark : bid;
  const ref = kind === "tp" ? Math.max(bid, m) : Math.min(bid, m);
  return legBounds(kind, { tpCents: legs.tpCents, slCents: legs.slCents, bid: ref, entry: null });
}

/** Active bracket of a position (bracket draft wins while dragging / editing). */
export function bracketOf(
  positionId: string,
  brackets: Record<string, Bracket>,
  drafts: Record<string, BracketInput>,
): BracketInput | null {
  const d = drafts[positionId];
  if (d) return d;
  for (const b of Object.values(brackets)) if (b.positionId === positionId && b.status === "active") return b;
  return null;
}

/** The position's bracket that already triggered and whose exit is still working (newest), else null. */
export function exitingBracket(positionId: string, brackets: Record<string, Bracket>): Bracket | null {
  let best: Bracket | null = null;
  for (const b of Object.values(brackets)) {
    if (b.positionId !== positionId || (b.status !== "active" && b.status !== "triggered")) continue;
    if (!best || b.updatedTs > best.updatedTs) best = b;
  }
  return best && best.status === "triggered" ? best : null;
}

const TRIGGER_WORD: Partial<Record<string, string>> = { tp: "TP", sl: "SL", btc_stop: "BTC stop", time_exit: "Time exit" };

/**
 * What a PUT bracket response changes in the store's brackets. `saved` is upserted unless the store already
 * holds a newer state of it (a WS triggered / cancelled / edit that overtook the response). `saved` null
 * (removed) retires the position's active brackets, except ones written after the request went out
 * (`sentAt`), which are not the bracket it removed.
 */
export function savedBracketUpdates(
  brackets: Record<string, Bracket>,
  positionId: string,
  saved: Bracket | null,
  sentAt: number,
  now: number,
): Bracket[] {
  if (saved) {
    const have = brackets[saved.id];
    return !have || saved.updatedTs >= have.updatedTs ? [saved] : [];
  }
  return Object.values(brackets)
    .filter((b) => b.positionId === positionId && b.status === "active" && b.updatedTs <= sentAt)
    .map((b) => ({ ...b, status: "cancelled" as const, updatedTs: now }));
}

/**
 * "+TP" / "+SL" starting value for a position that has no such leg: a quarter of the way from the bid
 * (or the entry when there is no quote) towards 99¢ / 1¢, at least 3¢ away, kept inside the drag range.
 */
export function defaultLeg(kind: LegKind, ref: number, b: Bounds | null): number | null {
  if (!b) return null;
  const e = Math.round(ref);
  const v =
    kind === "tp"
      ? e + Math.max(3, Math.round((MAX_PRICE_CENTS - e) / 4))
      : e - Math.max(3, Math.round((e - MIN_PRICE_CENTS) / 4));
  return Math.min(b.hi, Math.max(b.lo, v));
}

// ---- building the lines -------------------------------------------------------------------------------

export interface TicketPreview {
  side: Side;
  /** estimated average entry, cents (null = no quote yet) */
  entryCents: number | null;
  /** estimated contracts */
  qty: number;
  /** current bid of the side */
  bid: number | null;
  /** null = leg off / invalid */
  tpCents: number | null;
  slCents: number | null;
}

export interface LinesInput {
  /** only positions of this window are drawn (the chart shows the current market) */
  windowId: string | null;
  positions: readonly Position[];
  brackets: Record<string, Bracket>;
  drafts: Record<string, BracketInput>;
  /** newest first (store order); used for the entry triangles */
  fills: readonly Fill[];
  /** ticket bracket preview, null when the ticket bracket is off */
  ticket: TicketPreview | null;
  /** fresh best bid per side (null = no quote or a stale one: position legs cannot be moved / added) */
  bids: Record<Side, number | null>;
}

const MARKERS_PER_POSITION = 24;

/** Buy fills that built `p` (same window / side / mode, at or after it opened). Oldest first. */
export function entryMarkers(p: Position, fills: readonly Fill[]): FillMarker[] {
  const out: FillMarker[] = [];
  for (const f of fills) {
    if (f.windowId !== p.windowId || f.side !== p.side || f.mode !== p.mode || f.action !== "buy") continue;
    if (f.ts < p.openedTs - 1000 || (p.closedTs != null && f.ts > p.closedTs)) continue;
    out.push({ ts: f.ts, cents: f.priceCents });
    if (out.length >= MARKERS_PER_POSITION) break;
  }
  if (!out.length) out.push({ ts: p.openedTs, cents: p.avgCents });
  return out.sort((a, b) => a.ts - b.ts);
}

const sideWord = (s: Side) => (s === "up" ? "UP" : "DOWN");
const legWord = (k: LegKind) => (k === "tp" ? "Take profit" : "Stop loss");

function legLine(
  kind: LegKind,
  cents: number,
  o: { key: string; draft: boolean; positionId: string | null; side: Side; qty: number; entry: number | null; bounds: Bounds | null },
): PositionLine {
  const pnl = o.entry == null || o.qty <= 0 ? null : pnlUsd(o.qty, o.entry, cents);
  const runs: LabelRun[] = [
    { text: kind === "tp" ? "TP " : "SL ", tone: kind },
    { text: `${fmtPrice(cents)}¢ `, tone: kind },
    { text: fmtPnl(pnl), tone: pnlTone(pnl) },
  ];
  if (o.draft) runs.push({ text: " est", tone: "muted" });
  const what = o.draft ? `${legWord(kind)} for the next order` : `${legWord(kind)} for ${sideWord(o.side)} position`;
  return {
    key: o.key,
    kind,
    draft: o.draft,
    positionId: o.positionId,
    side: o.side,
    cents,
    runs,
    aria: `${what}: ${fmtPrice(cents)}¢, projected ${fmtPnl(pnl)}`,
    bounds: o.bounds,
    markers: [],
    addTp: false,
    addSl: false,
  };
}

/** Every line to draw: per position entry, TP, SL; then the ticket preview legs. */
export function buildLines(i: LinesInput): PositionLine[] {
  const out: PositionLine[] = [];
  if (i.windowId != null) {
    for (const p of i.positions) {
      if (p.status !== "open" || p.windowId !== i.windowId || p.qty <= 0) continue;
      // a triggered bracket: its exit order is working, nothing to edit (the backend refuses changes)
      const exiting = exitingBracket(p.id, i.brackets);
      const b = exiting ? null : bracketOf(p.id, i.brackets, i.drafts);
      const tp = b?.tpCents ?? null;
      const sl = b?.slCents ?? null;
      const bid = i.bids[p.side];
      const pnl = p.unrealizedPnlUsd ?? (p.markCents != null ? pnlUsd(p.qty, p.avgCents, p.markCents) : null);
      const exitText = exiting ? `${TRIGGER_WORD[exiting.triggerReason ?? ""] ?? "Bracket"} triggered · exiting` : null;
      out.push({
        key: `${p.id}:entry`,
        kind: "entry",
        draft: false,
        positionId: p.id,
        side: p.side,
        cents: p.avgCents,
        runs: [
          { text: `${sideWord(p.side)} `, tone: p.side === "up" ? "side-up" : "side-down" },
          { text: `${fmtQty(p.qty)} @ ${fmtPrice(p.avgCents)}¢ `, tone: "entry" },
          { text: fmtPnl(pnl), tone: pnlTone(pnl) },
          ...(exitText ? [{ text: ` ${exitText}`, tone: "muted" as const }] : []),
        ],
        aria:
          `${sideWord(p.side)} position: ${fmtQty(p.qty)} contracts at ${fmtPrice(p.avgCents)}¢, P&L ${fmtPnl(pnl)}` +
          (exitText ? `, ${exitText}` : ""),
        bounds: null,
        markers: entryMarkers(p, i.fills),
        addTp: !exiting && tp == null && positionLegBounds("tp", { tpCents: null, slCents: sl }, bid, p.markCents) != null,
        addSl: !exiting && sl == null && positionLegBounds("sl", { tpCents: tp, slCents: null }, bid, p.markCents) != null,
      });
      const base = { positionId: p.id, side: p.side, qty: p.qty, entry: p.avgCents, draft: false };
      const legs = { tpCents: tp, slCents: sl };
      if (tp != null) out.push(legLine("tp", tp, { ...base, key: `${p.id}:tp`, bounds: positionLegBounds("tp", legs, bid, p.markCents) }));
      if (sl != null) out.push(legLine("sl", sl, { ...base, key: `${p.id}:sl`, bounds: positionLegBounds("sl", legs, bid, p.markCents) }));
    }
  }
  const t = i.ticket;
  if (t) {
    const ctx: LegContext = { tpCents: t.tpCents, slCents: t.slCents, bid: t.bid, entry: t.entryCents };
    const base = { positionId: null, side: t.side, qty: t.qty, entry: t.entryCents, draft: true };
    if (t.tpCents != null) out.push(legLine("tp", t.tpCents, { ...base, key: "draft:tp", bounds: legBounds("tp", ctx) }));
    if (t.slCents != null) out.push(legLine("sl", t.slCents, { ...base, key: "draft:sl", bounds: legBounds("sl", ctx) }));
  }
  return out;
}

/** Left-axis tag text of a line: Down lines are marked, the scale is read against the Up odds. */
export function axisTagText(line: Pick<PositionLine, "side" | "cents">): string {
  const r = Math.round(line.cents * 10) / 10;
  const c = `${Number.isInteger(r) ? r.toFixed(0) : r.toFixed(1)}¢`;
  return line.side === "down" ? `D ${c}` : c;
}

/** A ticket TP/SL text field as a drawable price (1–99¢), else null. */
export function draftLegCents(on: boolean, text: string): number | null {
  if (!on) return null;
  const t = text.trim();
  if (t === "" || t === ".") return null;
  const v = Number(t);
  return Number.isFinite(v) && v >= MIN_PRICE_CENTS && v <= MAX_PRICE_CENTS ? v : null;
}

// ---- label layout -------------------------------------------------------------------------------------

export interface LabelBox {
  key: string;
  /** line y (label is vertically centred on it), pane px */
  y: number;
  w: number;
}

export interface PlacedLabel {
  key: string;
  left: number;
  top: number;
}

/**
 * Right-align labels against `right`; a label that would overlap one already placed slides left past it
 * (TradingView stacks position labels the same way), so every label stays centred on its own line.
 * Items are placed in the given order (earlier = closer to `right`). Never placed left of `minLeft`.
 */
export function layoutLabels(items: readonly LabelBox[], right: number, h: number, gap = 6, minLeft = 4): PlacedLabel[] {
  const placed: { key: string; left: number; top: number; w: number }[] = [];
  for (const it of items) {
    const top = Math.round(it.y - h / 2);
    let r = right;
    // slide left until free; each pass moves past one blocker, so this ends after <= placed.length passes
    for (let pass = 0; pass <= placed.length; pass++) {
      const left = r - it.w;
      const hit = placed.find((p) => top < p.top + h + 1 && p.top < top + h + 1 && left < p.left + p.w + gap && p.left < r + gap);
      if (!hit) break;
      r = hit.left - gap;
    }
    const left = Math.max(minLeft, Math.round(r - it.w));
    placed.push({ key: it.key, left, top, w: it.w });
  }
  return placed.map(({ key, left, top }) => ({ key, left, top }));
}
