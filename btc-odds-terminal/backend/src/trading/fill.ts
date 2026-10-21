// Pure paper-fill math for the backend (no I/O, no clock). OWNER: trading-backend.
//
// shared/paper.ts simulateFill() walks a book with a USD budget (buy) or a contract count (sell); the backend
// also needs a contract-count BUY (limit orders rest for `qty = usd / limit` contracts, the way the exchange
// takes them), resting-order matching and the marketable price caps, so the walker lives here. The USD-budget
// buy is the same algorithm as simulateFill (same 0.1 contract step), so the web mock and the backend agree.

import type { BookLevel, OrderAction } from "../../../shared/protocol.ts";
import { QTY_STEP } from "../../../shared/paper.ts";

/** Prices are quoted in 0.1 cent steps (the engine's flipped book rounds to 0.1). */
export const PRICE_STEP = 0.1;
export const MIN_LIMIT_CENTS = 0.1;
export const MAX_LIMIT_CENTS = 99.9;
const EPS = 1e-9;

/** Round down to the 0.1 contract step (float-safe). */
export function floorQty(q: number): number {
  if (!(q > 0)) return 0;
  return Math.round(Math.floor(q / QTY_STEP + 1e-7) * QTY_STEP * 10) / 10;
}

/** Round to `dp` decimals (storage / wire precision). */
export function round(v: number, dp = 4): number {
  const f = 10 ** dp;
  return Math.round(v * f) / f;
}

/** Round a price to the 0.1 cent grid and clamp it into the tradeable range. */
export function clampLimit(c: number): number {
  const r = Math.round(c / PRICE_STEP) * PRICE_STEP;
  return round(Math.min(MAX_LIMIT_CENTS, Math.max(MIN_LIMIT_CENTS, r)), 1);
}

/** Marketable-limit price: buy = ask + slippage, sell = bid - slippage (clamped to 0.1..99.9). */
export function marketableLimit(action: OrderAction, refCents: number, maxSlippageCents: number): number {
  return clampLimit(action === "buy" ? refCents + maxSlippageCents : refCents - maxSlippageCents);
}

export interface WalkInput {
  action: OrderAction;
  /** levels best first: asks for a buy, bids for a sell */
  levels: readonly BookLevel[];
  /** worst acceptable price, cents (buy: <=, sell: >=); Infinity / -Infinity = uncapped */
  limitCents: number;
  /** contracts to trade (sell; or a contract-count buy) */
  qty?: number;
  /** USD to spend (buy). Ignored when `qty` is set. */
  budgetUsd?: number;
}

export interface WalkResult {
  /** per-level executions, best first */
  executions: BookLevel[];
  qty: number;
  /** USD paid (buy) / received (sell), fees excluded */
  notionalUsd: number;
  /** null when nothing traded */
  avgCents: number | null;
  /** contracts left (qty mode) */
  leftQty: number;
  /** USD left (budget mode) */
  leftUsd: number;
  /** why the walk stopped: the request is done / the next level is past the limit / the book ran out */
  stoppedBy: "done" | "limit" | "depth";
}

/**
 * Walk the book best-first up to the limit. Buy quantities are rounded down to 0.1 contract; a sell takes
 * exactly what it asks for (live positions may hold fractional sizes the exchange reported).
 */
export function walkBook(i: WalkInput): WalkResult {
  const byQty = i.qty != null;
  let leftQty = byQty ? Math.max(0, i.qty!) : 0;
  let leftUsd = byQty ? 0 : Math.max(0, i.budgetUsd ?? 0);
  const executions: BookLevel[] = [];
  let qty = 0;
  let notional = 0;
  let stoppedBy: WalkResult["stoppedBy"] = "depth";
  let idx = 0;
  const nextPrice = () => i.levels[idx]?.price ?? (Number.isFinite(i.limitCents) ? i.limitCents : MAX_LIMIT_CENTS);
  /** nothing left to trade: qty mode = all taken; budget mode = the rest cannot buy 0.1 contract at the next price */
  const done = () => (byQty ? leftQty <= EPS : floorQty((leftUsd * 100) / Math.max(MIN_LIMIT_CENTS, nextPrice())) <= 0);

  for (; idx < i.levels.length; idx++) {
    if (done()) {
      stoppedBy = "done";
      break;
    }
    const l = i.levels[idx]!;
    if (!(l.size > 0) || !(l.price > 0)) continue;
    const within = i.action === "buy" ? l.price <= i.limitCents + EPS : l.price >= i.limitCents - EPS;
    if (!within) {
      stoppedBy = "limit";
      break;
    }
    let take: number;
    if (i.action === "sell") take = Math.min(leftQty, l.size);
    else if (byQty) take = floorQty(Math.min(leftQty, l.size));
    else take = floorQty(Math.min((leftUsd * 100) / l.price, l.size));
    if (take <= 0) continue; // dust level (< 0.1 contract): skip it, the next level may still fill
    executions.push({ price: l.price, size: round(take, 6) });
    qty += take;
    const usd = (take * l.price) / 100;
    notional += usd;
    if (byQty) leftQty -= take;
    else leftUsd -= usd;
  }
  if (idx >= i.levels.length && done()) stoppedBy = "done";
  qty = round(qty, 6);
  return {
    executions,
    qty,
    notionalUsd: round(notional, 6),
    avgCents: qty > 0 ? round((notional * 100) / qty, 6) : null,
    leftQty: round(Math.max(0, leftQty), 6),
    leftUsd: round(Math.max(0, leftUsd), 6),
    stoppedBy,
  };
}

/**
 * Contracts a resting paper limit order can take from one book snapshot: the size of every opposite level that
 * crosses the limit (asks <= limit for a buy, bids >= limit for a sell), capped by what is left on the order.
 * Resting fills execute AT the limit price (we were the maker; the crossing side traded into us).
 */
export function restingFillQty(action: OrderAction, levels: readonly BookLevel[], limitCents: number, remaining: number): number {
  let avail = 0;
  for (const l of levels) {
    if (!(l.size > 0)) continue;
    const crosses = action === "buy" ? l.price <= limitCents + EPS : l.price >= limitCents - EPS;
    if (!crosses) break;
    avail += l.size;
  }
  const q = Math.min(avail, remaining);
  return action === "buy" ? floorQty(q) : round(q, 6);
}

/** Price key for per-level bookkeeping (prices sit on a 0.1 cent grid). */
export const levelKey = (price: number) => Math.round(price * 1000) / 1000;

export interface FreshCrossing {
  /** contracts the order takes from this snapshot */
  qty: number;
  /** per crossing level: contracts taken (also added to `used`) */
  takes: Map<number, number>;
  /** the order's new baseline: crossing level sizes in this snapshot */
  seen: Map<number, number>;
}

/**
 * Resting paper limit matching that never re-uses liquidity: a crossing level only offers the size it GREW by
 * since the order last saw it (`seen`, the order's baseline: the snapshot it was placed / last matched against,
 * whose crossing size it already took or saw go to another order), minus what earlier orders took from the
 * same snapshot (`used`, shared across the resting orders of one side, oldest first). An unchanged book
 * therefore fills nothing; a level that vanishes and comes back counts as new.
 */
export function freshCrossingQty(
  action: OrderAction,
  levels: readonly BookLevel[],
  limitCents: number,
  remaining: number,
  seen: ReadonlyMap<number, number>,
  used: Map<number, number>,
): FreshCrossing {
  const takes = new Map<number, number>();
  const next = new Map<number, number>();
  let left = Math.max(0, remaining);
  let qty = 0;
  for (const l of levels) {
    if (!(l.size > 0)) continue;
    const crosses = action === "buy" ? l.price <= limitCents + EPS : l.price >= limitCents - EPS;
    if (!crosses) break;
    const k = levelKey(l.price);
    next.set(k, l.size);
    const fresh = Math.max(0, l.size - (seen.get(k) ?? 0));
    const avail = Math.max(0, Math.min(fresh, l.size - (used.get(k) ?? 0)));
    const raw = Math.min(avail, left);
    const take = action === "buy" ? floorQty(raw) : round(raw, 6);
    if (!(take > 0)) continue;
    takes.set(k, take);
    used.set(k, round((used.get(k) ?? 0) + take, 6));
    left -= take;
    qty += take;
  }
  return { qty: round(qty, 6), takes, seen: next };
}

/** Contracts a USD amount buys at a limit price (what a resting limit buy asks the exchange for). */
export function qtyForUsd(usd: number, priceCents: number): number {
  return priceCents > 0 ? floorQty((usd * 100) / priceCents) : 0;
}
