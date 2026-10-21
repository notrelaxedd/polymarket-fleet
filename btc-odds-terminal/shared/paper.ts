// Pure paper-trading math shared by the backend watcher (backend/src/trading) and the web mock / order ticket.
// OWNER: trading-backend. No I/O, no Date.now(): callers pass `now`. Imports are extensionless so both the
// backend (tsx / vitest) and Next.js resolve them.

import type { BookLevel, BookLevels, BracketInput, OrderAction, OrderReason, Side } from "./protocol";

/** Contract quantities are rounded down to this step (0.1 contract). */
export const QTY_STEP = 0.1;
export const MIN_PRICE_CENTS = 1;
export const MAX_PRICE_CENTS = 99;

const floorQty = (q: number) => Math.floor(q / QTY_STEP + 1e-9) * QTY_STEP;
const round2 = (v: number) => Math.round(v * 100) / 100;

/**
 * The book of the side being traded, best first. Up = the Up book as is. Down is derived from the Up book:
 * Down bids = 100 - Up asks, Down asks = 100 - Up bids (sizes carry over).
 */
export function sideBook(up: Pick<BookLevels, "bids" | "asks">, side: Side): { bids: BookLevel[]; asks: BookLevel[] } {
  if (side === "up") return { bids: up.bids.slice(), asks: up.asks.slice() };
  return {
    bids: up.asks.map((l) => ({ price: 100 - l.price, size: l.size })),
    asks: up.bids.map((l) => ({ price: 100 - l.price, size: l.size })),
  };
}

export interface SimFillInput {
  action: OrderAction;
  /** levels to walk, best first: asks for a buy, bids for a sell */
  levels: BookLevel[];
  /** buy: USD to spend (takes precedence over qty for a buy) */
  usd?: number;
  /** sell: contracts to sell; buy without `usd`: contracts to buy */
  qty?: number;
  /** worst acceptable price, cents (buy: <=, sell: >=) */
  limitCents: number;
  /** cents per contract, null = unknown ([FEE]) */
  feeCentsPerContract: number | null;
}

export interface SimFillResult {
  qty: number;
  /** null when nothing filled */
  avgCents: number | null;
  /** USD paid (buy) or received (sell), fees excluded */
  notionalUsd: number;
  /** null = unknown fee */
  feeUsd: number | null;
  /** per-level executions, best first */
  executions: BookLevel[];
  /** avg vs best level, cents, positive = worse; null when nothing filled */
  slippageCents: number | null;
  /** the whole request filled within the limit */
  complete: boolean;
}

/**
 * Walk the book up to the limit (paper fills), in 0.1-contract steps. A buy spends `usd` (or buys `qty` contracts
 * when no usd is given); a sell sells `qty`. A level smaller than one step is skipped (like the backend's walk),
 * the walk stops at the first level past the limit or when the budget is used up.
 */
export function simulateFill(i: SimFillInput): SimFillResult {
  const execs: BookLevel[] = [];
  let qty = 0;
  let notional = 0;
  const byUsd = i.action === "buy" && i.usd != null;
  let budget = byUsd ? Math.max(0, i.usd ?? 0) : Math.max(0, i.qty ?? 0);
  for (const l of i.levels) {
    if (budget <= 1e-9) break;
    const ok = i.action === "buy" ? l.price <= i.limitCents : l.price >= i.limitCents;
    if (!ok) break;
    const want = byUsd ? floorQty(budget / (l.price / 100)) : floorQty(budget);
    if (want <= 0) break; // budget left is less than one step at this price
    const take = Math.min(want, floorQty(l.size));
    if (take <= 0) continue; // dust level: skip it
    execs.push({ price: l.price, size: take });
    qty += take;
    notional += (take * l.price) / 100;
    budget -= byUsd ? (take * l.price) / 100 : take;
  }
  qty = Math.round(qty * 10) / 10;
  const avg = qty > 0 ? (notional * 100) / qty : null;
  const best = i.levels[0]?.price ?? null;
  const fee = i.feeCentsPerContract == null ? null : round2((i.feeCentsPerContract * qty) / 100);
  const complete = byUsd ? budget < (i.levels[0]?.price ?? 100) / 100 / 10 + 1e-9 : budget <= 1e-9;
  return {
    qty,
    avgCents: avg,
    notionalUsd: round2(notional),
    feeUsd: fee,
    executions: execs,
    slippageCents: avg != null && best != null ? (i.action === "buy" ? avg - best : best - avg) : null,
    complete,
  };
}

export interface BracketMarket {
  now: number;
  /** best bid of the position's side, cents */
  sideBid: number | null;
  /** BRTI proxy, USD */
  btc: number | null;
  windowEndTs: number;
}

/**
 * Which bracket leg (if any) triggers now. OCO: the first leg that triggers wins; the caller then cancels the
 * bracket. Priority when several trigger in the same evaluation: sl, btc_stop, tp, time_exit.
 * Baseline implementation; trading-backend owns + tests it.
 */
export function evaluateBracket(b: BracketInput, side: Side, m: BracketMarket): OrderReason | null {
  const bid = m.sideBid;
  if (b.slCents != null && bid != null && bid <= b.slCents) return "sl";
  if (b.btcStop != null && m.btc != null && (side === "up" ? m.btc < b.btcStop : m.btc > b.btcStop)) return "btc_stop";
  if (b.tpCents != null && bid != null && bid >= b.tpCents) return "tp";
  if (b.timeExitSec != null && (m.windowEndTs - m.now) / 1000 <= b.timeExitSec) return "time_exit";
  return null;
}

/** P&L in USD of `qty` contracts bought at `entryCents` and exited at `exitCents` (fees excluded). */
export function pnlUsd(qty: number, entryCents: number, exitCents: number): number {
  return round2((qty * (exitCents - entryCents)) / 100);
}

/** True when every leg is null (= no bracket). */
export function bracketEmpty(b: BracketInput | null | undefined): boolean {
  return !b || (b.tpCents == null && b.slCents == null && b.btcStop == null && b.timeExitSec == null);
}

export const clampCents = (c: number) => Math.min(MAX_PRICE_CENTS, Math.max(MIN_PRICE_CENTS, c));
