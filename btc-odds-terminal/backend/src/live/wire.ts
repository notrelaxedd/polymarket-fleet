// Polymarket US trading wire shapes + defensive parsers (official SDK polymarket-us-typescript @abdcaae:
// src/types/{orders,portfolio,account,common}.ts, src/websocket/{private,types}.ts). Money on the wire is an
// Amount { value: "0.55", currency: "USD" } in DOLLARS; everything returned from here uses CENTS for contract
// prices and USD for money. Quantities are contracts (numbers or decimal strings on the wire).

import type { OrderStatus } from "../../../shared/protocol.ts";
import { isObj, num, toCents, toMs } from "../polymarket/normalize.ts";

export type OrderIntent = "ORDER_INTENT_BUY_LONG" | "ORDER_INTENT_SELL_LONG" | "ORDER_INTENT_BUY_SHORT" | "ORDER_INTENT_SELL_SHORT";
export type TimeInForce =
  | "TIME_IN_FORCE_GOOD_TILL_CANCEL"
  | "TIME_IN_FORCE_GOOD_TILL_DATE"
  | "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
  | "TIME_IN_FORCE_FILL_OR_KILL";
export type ManualOrderIndicator = "MANUAL_ORDER_INDICATOR_MANUAL" | "MANUAL_ORDER_INDICATOR_AUTOMATIC";

export interface Amount {
  value: string;
  currency: "USD";
}

/** POST /v1/orders body (SDK CreateOrderParams; only the fields the terminal sends). */
export interface CreateOrderBody {
  marketSlug: string;
  intent: OrderIntent;
  type: "ORDER_TYPE_LIMIT" | "ORDER_TYPE_MARKET";
  price?: Amount;
  quantity?: number;
  tif?: TimeInForce;
  manualOrderIndicator?: ManualOrderIndicator;
  synchronousExecution?: boolean;
}

/** The two sides of one exchange market. */
export type InstSide = "long" | "short";

export const intentOf = (action: "buy" | "sell", side: InstSide): OrderIntent =>
  `ORDER_INTENT_${action === "buy" ? "BUY" : "SELL"}_${side === "long" ? "LONG" : "SHORT"}` as OrderIntent;

export function intentSide(intent: unknown): InstSide | null {
  if (typeof intent !== "string") return null;
  if (intent.endsWith("_SHORT")) return "short";
  if (intent.endsWith("_LONG")) return "long";
  return null;
}

/** Dollars string for an Amount: cents -> "0.55" (at most 4 decimals, trailing zeros trimmed, at least 2). */
export function centsToDollars(cents: number): string {
  const s = (Math.round(cents * 100) / 10_000).toFixed(4).replace(/0+$/, "");
  const [i, f = ""] = s.split(".");
  return `${i}.${f.padEnd(2, "0")}`;
}

export const usd = (cents: number): Amount => ({ value: centsToDollars(cents), currency: "USD" });

/** Amount / number / string in DOLLARS -> USD number (not cents). */
export function money(v: unknown): number | null {
  return num(v);
}

// ---------------------------------------------------------------- orders / executions

export interface WireOrder {
  id: string;
  marketSlug: string | null;
  intent: string | null;
  /** ORDER_STATE_* */
  state: string | null;
  /** limit price, cents in the exchange's quote terms */
  priceCents: number | null;
  quantity: number | null;
  /** cumulative filled contracts */
  cumQuantity: number | null;
  leavesQuantity: number | null;
  /** average fill price, cents (exchange quote terms) */
  avgCents: number | null;
  /** cumulative commission collected, USD */
  commissionUsd: number | null;
  createTs: number | null;
}

export interface WireExecution {
  id: string | null;
  order: WireOrder | null;
  /** EXECUTION_TYPE_* */
  type: string | null;
  lastShares: number | null;
  lastCents: number | null;
  commissionUsd: number | null;
  ts: number | null;
  /** reject reason / free text */
  text: string | null;
}

const s = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

/**
 * Dollars (or cents > 1) -> cents WITHOUT the 0.1c rounding of toCents: an order's avgPx feeds the implied price of a
 * delta fill, (cum * avg - applied) / delta, where a rounded avg is off by up to 0.05c * cum / delta.
 */
export function exactCents(v: unknown): number | null {
  const n = num(v);
  if (n === null || n < 0) return null;
  const cents = n > 1 ? n : n * 100;
  if (cents > 100) return null;
  return Math.round(cents * 1e6) / 1e6; // float noise only
}

export function parseOrder(raw: unknown): WireOrder | null {
  if (!isObj(raw)) return null;
  const id = s(raw.id);
  if (!id) return null;
  return {
    id,
    marketSlug: s(raw.marketSlug) ?? (isObj(raw.marketMetadata) ? s(raw.marketMetadata.slug) : null),
    intent: s(raw.intent),
    state: s(raw.state),
    priceCents: toCents(raw.price),
    quantity: num(raw.quantity),
    cumQuantity: num(raw.cumQuantity),
    leavesQuantity: num(raw.leavesQuantity),
    avgCents: exactCents(raw.avgPx),
    commissionUsd: money(raw.commissionNotionalTotalCollected),
    createTs: toMs(raw.createTime ?? raw.insertTime),
  };
}

export function parseExecution(raw: unknown): WireExecution | null {
  if (!isObj(raw)) return null;
  const order = parseOrder(raw.order);
  if (!order && !s(raw.id)) return null;
  return {
    id: s(raw.id),
    order,
    type: s(raw.type),
    lastShares: num(raw.lastShares),
    lastCents: toCents(raw.lastPx),
    commissionUsd: money(raw.commissionNotionalCollected),
    ts: toMs(raw.transactTime),
    text: s(raw.orderRejectReason) ?? s(raw.text),
  };
}

/** ORDER_STATE_* -> the terminal's OrderStatus. Unknown / missing -> null. */
export function mapOrderState(state: string | null | undefined): OrderStatus | null {
  switch (state) {
    case "ORDER_STATE_PENDING_NEW":
    case "ORDER_STATE_PENDING_RISK":
      return "pending";
    case "ORDER_STATE_NEW":
    case "ORDER_STATE_PENDING_REPLACE":
    case "ORDER_STATE_REPLACED":
    case "ORDER_STATE_PENDING_CANCEL":
      return "open";
    case "ORDER_STATE_PARTIALLY_FILLED":
      return "partial";
    case "ORDER_STATE_FILLED":
      return "filled";
    case "ORDER_STATE_CANCELED":
    case "ORDER_STATE_EXPIRED":
      return "cancelled";
    case "ORDER_STATE_REJECTED":
      return "rejected";
    default:
      return null;
  }
}

/** EXECUTION_TYPE_* -> OrderStatus (used when the execution's order carries no state). */
export function mapExecutionType(type: string | null | undefined): OrderStatus | null {
  switch (type) {
    case "EXECUTION_TYPE_NEW":
    case "EXECUTION_TYPE_REPLACE":
      return "open";
    case "EXECUTION_TYPE_PARTIAL_FILL":
      return "partial";
    case "EXECUTION_TYPE_FILL":
      return "filled";
    case "EXECUTION_TYPE_CANCELED":
    case "EXECUTION_TYPE_EXPIRED":
    case "EXECUTION_TYPE_DONE_FOR_DAY":
      return "cancelled";
    case "EXECUTION_TYPE_REJECTED":
      return "rejected";
    default:
      return null;
  }
}

export const TERMINAL: ReadonlySet<OrderStatus> = new Set(["filled", "cancelled", "rejected"]);

// ---------------------------------------------------------------- positions / balances

export interface WirePosition {
  slug: string;
  /** signed: > 0 long holding, < 0 short holding (sign of the exchange's netPosition) */
  net: number;
  /**
   * average entry, cents; null when not derivable. In the exchange's quote terms when read from avgPx / costPerShare,
   * but already in HELD-instrument terms when derived from cost / |net| (see avgHeldSide).
   */
  avgCents: number | null;
  /** avgCents came from cost / |net|: the price paid for the held instrument, never re-quoted */
  avgHeldSide: boolean;
  costUsd: number | null;
  realizedUsd: number | null;
  expired: boolean;
}

/** One UserPosition (prefers the exact *Decimal quantity strings). null when netPosition is unparseable. */
export function parseUserPosition(slug: string, raw: unknown): WirePosition | null {
  if (!isObj(raw)) return null;
  const net = num(raw.netPositionDecimal) ?? num(raw.netPosition);
  if (net === null) return null;
  const cost = money(raw.cost);
  let avg = toCents(raw.avgPx) ?? toCents(raw.costPerShare);
  let avgHeldSide = false;
  if (avg === null && cost !== null && net !== 0) {
    const c = (Math.abs(cost) / Math.abs(net)) * 100;
    avg = c >= 0 && c <= 100 ? Math.round(c * 10) / 10 : null;
    avgHeldSide = avg !== null;
  }
  const metaSlug = isObj(raw.marketMetadata) ? s(raw.marketMetadata.slug) : null;
  return {
    slug: metaSlug ?? slug,
    net,
    avgCents: avg,
    avgHeldSide,
    costUsd: cost,
    realizedUsd: money(raw.realized),
    expired: raw.expired === true,
  };
}

/**
 * GET /v1/portfolio/positions page -> positions + paging. A row with an unparseable netPosition is never turned into
 * a flat position: it is left out and its slug listed in `unparseable`, so the caller can fail only when it matters
 * (a market the terminal trades) instead of one stray row disabling every reconcile.
 */
export function parsePositionsPage(raw: unknown): { positions: WirePosition[]; unparseable: string[]; nextCursor: string | null; eof: boolean } {
  if (!isObj(raw)) return { positions: [], unparseable: [], nextCursor: null, eof: true };
  const out: WirePosition[] = [];
  const unparseable: string[] = [];
  const map = isObj(raw.positions) ? raw.positions : {};
  for (const [slug, p] of Object.entries(map)) {
    const parsed = parseUserPosition(slug, p);
    if (parsed) out.push(parsed);
    else unparseable.push(isObj(p) && isObj(p.marketMetadata) ? (s(p.marketMetadata.slug) ?? slug) : slug);
  }
  return { positions: out, unparseable, nextCursor: s(raw.nextCursor), eof: raw.eof === true };
}

export interface WireBalance {
  currency: string;
  balanceUsd: number | null;
  buyingPowerUsd: number | null;
}

export function parseBalance(raw: unknown): WireBalance | null {
  if (!isObj(raw)) return null;
  // current shape: { currentBalance, buyingPower }; legacy WS shape: { balance, buyingPower }
  const balanceUsd = num(raw.currentBalance) ?? num(raw.balance);
  const buyingPowerUsd = num(raw.buyingPower);
  if (balanceUsd === null && buyingPowerUsd === null) return null;
  return { currency: s(raw.currency) ?? "USD", balanceUsd, buyingPowerUsd };
}

/** The USD balance among GET /v1/account/balances `balances` (first entry when none says USD). */
export function pickUsdBalance(list: unknown): WireBalance | null {
  const arr = Array.isArray(list) ? list.map(parseBalance).filter((b): b is WireBalance => b !== null) : [];
  return arr.find((b) => b.currency.toUpperCase() === "USD") ?? arr[0] ?? null;
}

// ---------------------------------------------------------------- private WebSocket frames

export type PrivateFrame =
  | { kind: "heartbeat" }
  | { kind: "error"; error: string; requestId: string | null }
  | { kind: "orderSnapshot"; orders: WireOrder[]; eof: boolean; requestId: string | null }
  | { kind: "execution"; execution: WireExecution }
  | { kind: "position"; slug: string | null; after: WirePosition | null }
  | { kind: "balance"; balance: WireBalance | null }
  | { kind: "other" };

/** Classify one private-WS frame (current + legacy envelope keys, as the SDK's PrivateWebSocket.handleMessage). */
export function parsePrivateFrame(text: string): PrivateFrame {
  let m: unknown;
  try {
    m = JSON.parse(text);
  } catch {
    return { kind: "other" };
  }
  if (!isObj(m)) return { kind: "other" };
  const requestId = s(m.requestId);
  if ("heartbeat" in m) return { kind: "heartbeat" };
  if (typeof m.error === "string" && m.error) return { kind: "error", error: m.error, requestId };

  const snap = isObj(m.orderSubscriptionSnapshot) ? m.orderSubscriptionSnapshot : isObj(m.ordersSnapshot) ? m.ordersSnapshot : null;
  if (snap) {
    const orders = Array.isArray(snap.orders) ? snap.orders.map(parseOrder).filter((o): o is WireOrder => o !== null) : [];
    return { kind: "orderSnapshot", orders, eof: snap.eof === true, requestId };
  }
  const upd = isObj(m.orderSubscriptionUpdate) ? m.orderSubscriptionUpdate : isObj(m.orderUpdate) ? m.orderUpdate : null;
  if (upd) {
    const execution = parseExecution(upd.execution);
    return execution ? { kind: "execution", execution } : { kind: "other" };
  }
  if (isObj(m.positionSubscription)) {
    const p = m.positionSubscription;
    const pick = isObj(p.afterPosition) ? p.afterPosition : isObj(p.beforePosition) ? p.beforePosition : null;
    const slug = pick && isObj(pick.marketMetadata) ? s(pick.marketMetadata.slug) : null;
    const after = isObj(p.afterPosition) ? parseUserPosition(slug ?? "", p.afterPosition) : null;
    return { kind: "position", slug: after?.slug || slug, after };
  }
  const legacyPos = isObj(m.positionSubscriptionUpdate) ? m.positionSubscriptionUpdate : isObj(m.positionUpdate) ? m.positionUpdate : null;
  if (legacyPos) {
    const slug = s(legacyPos.marketSlug);
    return { kind: "position", slug, after: parseUserPosition(slug ?? "", legacyPos.position) };
  }
  if (isObj(m.positionSubscriptionSnapshot) || isObj(m.positionsSnapshot)) return { kind: "position", slug: null, after: null };

  if (isObj(m.accountBalancesSnapshot)) return { kind: "balance", balance: pickUsdBalance(m.accountBalancesSnapshot.balances) };
  if (isObj(m.accountBalancesUpdate)) {
    const ch = isObj(m.accountBalancesUpdate.balanceChange) ? m.accountBalancesUpdate.balanceChange : null;
    return { kind: "balance", balance: ch ? parseBalance(ch.afterBalance) : null };
  }
  const legacyBal = isObj(m.accountBalanceSubscriptionSnapshot)
    ? m.accountBalanceSubscriptionSnapshot
    : isObj(m.accountBalanceSubscriptionUpdate)
      ? m.accountBalanceSubscriptionUpdate
      : isObj(m.accountBalanceUpdate)
        ? m.accountBalanceUpdate
        : null;
  if (legacyBal) return { kind: "balance", balance: parseBalance(legacyBal) };
  return { kind: "other" };
}
