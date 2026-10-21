// Pure helpers for the Orders & fills and Window history tables. OWNER: tabs.
// Tested in src/components/tabs/__tests__/tabs-logic.test.ts.

import type { AnalysisScope, FairSnap, Fill, MarketWindow, Order, OrderReason, OrderStatus, OrderType, Position, TradingMode } from "@shared/protocol";

// ---- sample filter ----

/** Windows in the bottom-tab sample: newest first. This window = current; Last 20 = current + 20 before it. */
export function windowsInScope(scope: AnalysisScope, current: MarketWindow | null, all: readonly MarketWindow[], now: number): MarketWindow[] {
  const started = dedupe(all)
    .filter((w) => w.startTs <= now || w.id === current?.id)
    .sort((a, b) => b.startTs - a.startTs);
  if (scope === "window") return current ? [current] : started.slice(0, 1);
  if (scope === "last20") {
    const out = current ? [current, ...started.filter((w) => w.id !== current.id && w.startTs < current.startTs)] : started;
    return out.slice(0, 21);
  }
  return started;
}

/**
 * The window the sample filter is anchored to. While a replay runs, store.current is the replayed window; the
 * tables keep following the live one (the window marked live in the store), falling back to current.
 */
export function scopeAnchor(current: MarketWindow | null, windows: Record<string, MarketWindow>, replayActive: boolean): MarketWindow | null {
  if (!replayActive) return current;
  let live: MarketWindow | null = null;
  for (const w of Object.values(windows)) if (w.status === "live" && (!live || w.startTs > live.startTs)) live = w;
  return live ?? current;
}

function dedupe(ws: readonly MarketWindow[]): MarketWindow[] {
  const m = new Map<string, MarketWindow>();
  for (const w of ws) m.set(w.id, w);
  return [...m.values()];
}

/** Merge window lists by id; `preferred` (fresher, e.g. the store) wins. */
export function mergeWindows(preferred: readonly MarketWindow[], other: readonly MarketWindow[]): MarketWindow[] {
  return mergeById(preferred, other);
}

/**
 * Merge two lists by id. `preferred` (the store, kept fresh by WS upserts) wins, unless `updatedTs` says the
 * other copy is newer (a REST page fetched after the store's copy).
 */
export function mergeById<T extends { id: string; updatedTs?: number }>(preferred: readonly T[], other: readonly T[]): T[] {
  const m = new Map<string, T>();
  for (const x of other) m.set(x.id, x);
  for (const x of preferred) {
    const o = m.get(x.id);
    if (!o || o.updatedTs == null || x.updatedTs == null || x.updatedTs >= o.updatedTs) m.set(x.id, x);
  }
  return [...m.values()];
}

/**
 * Whether a newest-first REST page fetched with `limit` holds every row back to `sinceTs`: it does when the
 * page is not full, or when it already reaches past `sinceTs`. `sinceTs` null = the whole history is needed.
 */
export function pageCovers(tsList: readonly number[], limit: number, sinceTs: number | null): boolean {
  if (tsList.length < limit) return true;
  if (sinceTs == null) return false;
  let oldest = Infinity;
  for (const t of tsList) if (t < oldest) oldest = t;
  return oldest < sinceTs;
}

// ---- orders & fills ----

/** Fills grouped by order id, oldest first within each order. */
export function fillsByOrder(fills: readonly Fill[]): Map<string, Fill[]> {
  const m = new Map<string, Fill[]>();
  for (const f of fills) {
    const arr = m.get(f.orderId);
    if (arr) arr.push(f);
    else m.set(f.orderId, [f]);
  }
  for (const arr of m.values()) arr.sort((a, b) => a.ts - b.ts);
  return m;
}

/**
 * Average fill price vs the order's limit, cents, positive = worse than the limit (buy: paid above it, sell:
 * received below it). For a marketable order the limit is the slippage cap, so this is ≤ 0 = unused cap.
 */
export function slipVsLimit(o: Pick<Order, "action" | "limitCents" | "avgFillCents" | "filledQty">): number | null {
  if (o.limitCents == null || o.avgFillCents == null || !(o.filledQty > 0)) return null;
  const d = o.action === "buy" ? o.avgFillCents - o.limitCents : o.limitCents - o.avgFillCents;
  return Math.round(d * 100) / 100;
}

/** Quantity-weighted slippage of fills vs the reference price at submit, cents (positive = worse). */
export function fillSlippage(fills: readonly Pick<Fill, "qty" | "slippageCents">[]): number | null {
  let q = 0;
  let s = 0;
  for (const f of fills) {
    if (f.slippageCents == null || !Number.isFinite(f.slippageCents) || !(f.qty > 0)) continue;
    q += f.qty;
    s += f.slippageCents * f.qty;
  }
  return q > 0 ? Math.round((s / q) * 100) / 100 : null;
}

/**
 * True when some of an order's fills are not loaded (snapshot / store caps, a WS gap): the loaded fill
 * quantity is short of `filledQty`. Its fee and slippage are then unknown here, not $0.00 / "no slippage" —
 * the backend stores an unknown ([FEE]) fee as 0 in `Order.feesUsd`, so only the fills can tell.
 */
export function fillsMissing(o: Pick<Order, "filledQty">, fills: readonly Pick<Fill, "qty">[]): boolean {
  if (!(o.filledQty > 0)) return false;
  let q = 0;
  for (const f of fills) q += f.qty;
  return q < o.filledQty - 1e-4;
}

export interface FeeCell {
  usd: number | null;
  /** a loaded fill's fee is unknown (unconfirmed schedule, no override) -> "[FEE]" */
  unknown: boolean;
  /** fills not loaded -> "—" */
  missing: boolean;
}

/** Fee cell: "[FEE]" when any fill's fee is unknown, "—" when the order's fills are not (all) loaded. */
export function feeCell(o: Pick<Order, "feesUsd" | "filledQty">, fills: readonly Pick<Fill, "feeUsd" | "qty">[]): FeeCell {
  const unknown = fills.some((f) => f.feeUsd == null);
  const missing = !unknown && fillsMissing(o, fills);
  return { usd: unknown || missing ? null : o.feesUsd, unknown, missing };
}

export const REASON_LABEL: Record<OrderReason, string> = {
  manual: "Manual",
  close: "Close",
  tp: "Take profit",
  sl: "Stop loss",
  btc_stop: "BTC stop",
  time_exit: "Time exit",
  kill: "Kill switch",
};

export const TYPE_LABEL: Record<OrderType, string> = { limit: "Limit", marketable: "Marketable" };

export const STATUS_LABEL: Record<OrderStatus, string> = {
  pending: "Pending",
  open: "Open",
  partial: "Partial",
  filled: "Filled",
  cancelled: "Cancelled",
  rejected: "Rejected",
};

export const cancellable = (s: OrderStatus) => s === "open" || s === "partial";

/** Contract quantities to 1 decimal (QTY_STEP 0.1), no trailing ".0". */
export function qtyText(v: number): string {
  const r = Math.round(v * 10) / 10;
  return Number.isInteger(r) ? r.toFixed(0) : r.toFixed(1);
}

export interface OrdersSummary {
  orders: number;
  fills: number;
  /** quantity-weighted slippage across all fills (cents) */
  avgSlippage: number | null;
  feesUsd: number;
  feesUnknown: boolean;
  /** orders with filledQty > 0 whose fills are not (all) loaded: fees / slippage / fill count are partial */
  fillsMissing: number;
}

export function summarizeOrders(orders: readonly Order[], byOrder: Map<string, Fill[]>): OrdersSummary {
  const fills: Fill[] = [];
  let feesUsd = 0;
  let missing = 0;
  for (const o of orders) {
    const fs = byOrder.get(o.id) ?? [];
    fills.push(...fs);
    feesUsd += o.feesUsd;
    if (fillsMissing(o, fs)) missing++;
  }
  return {
    orders: orders.length,
    fills: fills.length,
    avgSlippage: fillSlippage(fills),
    feesUsd: Math.round(feesUsd * 100) / 100,
    feesUnknown: fills.some((f) => f.feeUsd == null),
    fillsMissing: missing,
  };
}

// ---- window history ----

export interface WindowPnl {
  usd: number;
  /** some fee may be unknown ([FEE], counted as 0 in realizedPnlUsd): the number may overstate P&L */
  feeUnknown: boolean;
}

/**
 * My P&L per window for ONE trading mode (paper and live are never summed): realized (net of known fees) +
 * unrealized of still-open positions, USD. `feeUnknown` when a loaded fill of that window / mode has an
 * unknown fee, or when the fee schedule is unconfirmed (`feeScheduleKnown` false) and the position shows no fee.
 */
export function pnlByWindow(
  positions: readonly Position[],
  mode: TradingMode,
  fills: readonly Pick<Fill, "windowId" | "mode" | "feeUsd">[] = [],
  feeScheduleKnown = true,
): Map<string, WindowPnl> {
  const unknownFee = new Set<string>();
  for (const f of fills) if (f.mode === mode && f.feeUsd == null) unknownFee.add(f.windowId);
  const m = new Map<string, WindowPnl>();
  for (const p of positions) {
    if (p.mode !== mode) continue;
    const v = p.realizedPnlUsd + (p.status === "open" ? p.unrealizedPnlUsd ?? 0 : 0);
    const prev = m.get(p.windowId);
    const feeUnknown = unknownFee.has(p.windowId) || (!feeScheduleKnown && !(p.feesUsd > 0));
    m.set(p.windowId, { usd: Math.round(((prev?.usd ?? 0) + v) * 100) / 100, feeUnknown: (prev?.feeUnknown ?? false) || feeUnknown });
  }
  return m;
}

/** positions in the snapshot: last 24h (backend trading RECENT_MS), at most 200 */
export const SNAPSHOT_POSITIONS_MS = 24 * 3600_000;
export const SNAPSHOT_POSITIONS_LIMIT = 200;

/**
 * Oldest window start whose positions are all loaded (null = all of them). `page` is GET /api/positions
 * (newest opened first) fetched with `limit`, or null when it was not (or could not be) fetched; then only the
 * snapshot's span is known.
 */
export function positionsCoverFrom(
  page: { rows: readonly Pick<Position, "openedTs">[]; limit: number } | null,
  store: readonly Pick<Position, "openedTs">[],
  now: number,
): number | null {
  const oldest = (rows: readonly Pick<Position, "openedTs">[]) => rows.reduce((m, p) => Math.min(m, p.openedTs), Infinity);
  if (page) return page.rows.length < page.limit ? null : oldest(page.rows);
  const from = now - SNAPSHOT_POSITIONS_MS;
  return store.length >= SNAPSHOT_POSITIONS_LIMIT ? Math.max(from, oldest(store)) : from;
}

/** Earliest fair snapshot timestamp in a series (null when empty): max edge is unknown before it. */
export function earliestTs(series: readonly { ts: number }[]): number | null {
  let t = Infinity;
  for (const x of series) if (x.ts < t) t = x.ts;
  return Number.isFinite(t) ? t : null;
}

/** Largest |edge| seen per window (signed), cents. */
export function maxEdgeByWindow(fair: readonly Pick<FairSnap, "windowId" | "edge">[], into = new Map<string, number>()): Map<string, number> {
  for (const f of fair) {
    if (f.edge == null || !Number.isFinite(f.edge)) continue;
    const prev = into.get(f.windowId);
    if (prev === undefined || Math.abs(f.edge) > Math.abs(prev)) into.set(f.windowId, f.edge);
  }
  return into;
}
