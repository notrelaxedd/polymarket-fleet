// Pure order-ticket math: book walk estimate, draft validation, request building, bracket suggestions and
// risk-bar tones. OWNER: trading-ui. No React, no store access (unit-tested in __tests__/ticket-math.test.ts).
import type {
  BookLevel,
  BookLevels,
  BracketInput,
  OddsSnap,
  PlaceOrderRequest,
  RiskLimits,
  RiskState,
  Side,
} from "@shared/protocol";
import { MAX_PRICE_CENTS, MIN_PRICE_CENTS, sideBook, simulateFill } from "@shared/paper";
import type { TicketDraft } from "@/lib/store";

/** Seconds in one market window (time exit must be below this). */
export const WINDOW_SEC = 15 * 60;
/** backend MIN_TIME_EXIT_SEC: the watcher checks once a second and the exit needs time to fill */
export const MIN_TIME_EXIT_SEC = 3;
export const QUICK_AMOUNTS = [10, 25, 50, 100] as const;

const round1 = (v: number) => Math.round(v * 10) / 10;
/** round down to the 0.1 contract step (float-safe) */
const floorQty = (q: number) => (q > 0 ? Math.round(Math.floor(q * 10 + 1e-7)) / 10 : 0);

/** Buy `qty` contracts walking asks best-first up to `limit` (contract-count walk; limit orders). */
function walkQty(levels: BookLevel[], qty: number, limit: number): { qty: number; notionalUsd: number } {
  let left = qty;
  let got = 0;
  let notional = 0;
  for (const l of levels) {
    if (left <= 1e-9 || l.price > limit + 1e-9) break;
    const take = Math.min(left, floorQty(l.size));
    if (take <= 0) continue;
    got += take;
    notional += (take * l.price) / 100;
    left = round1(left - take);
  }
  return { qty: round1(got), notionalUsd: round2(notional) };
}
const round2 = (v: number) => Math.round(v * 100) / 100;

/** "12.5" -> 12.5; "" / "." / garbage -> null. */
export function parseNum(s: string | null | undefined): number | null {
  if (s == null) return null;
  const t = s.trim();
  if (t === "" || t === ".") return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

/** Keep digits and one decimal point (input sanitizer). */
export function sanitizeDecimal(s: string): string {
  const clean = s.replace(/[^0-9.]/g, "");
  const i = clean.indexOf(".");
  return i < 0 ? clean : clean.slice(0, i + 1) + clean.slice(i + 1).replace(/\./g, "");
}

/** Best ask / bid of `side` from the Up quote (Down = mirrored). */
export function sideQuote(odds: Pick<OddsSnap, "upBid" | "upAsk" | "downBid" | "downAsk"> | null, side: Side) {
  if (!odds) return { bid: null, ask: null };
  return side === "up" ? { bid: odds.upBid, ask: odds.upAsk } : { bid: odds.downBid, ask: odds.downAsk };
}

export interface EstimateInput {
  side: Side;
  type: "limit" | "marketable";
  usd: number | null;
  /** limit text parsed; null = use the current ask */
  limitCents: number | null;
  maxSlippageCents: number | null;
  /** current Up book depth (preferred), else top of book from `odds` */
  book: Pick<BookLevels, "bids" | "asks"> | null;
  odds: Pick<OddsSnap, "upBid" | "upAsk" | "downBid" | "downAsk"> | null;
  /** fair P(Up), cents */
  fairUp: number | null;
  feeCentsPerContract: number | null;
}

export interface TicketEstimate {
  /** best ask / bid of the selected side */
  ask: number | null;
  bid: number | null;
  /** worst price the order accepts (marketable: ask + slippage cap) */
  limitCents: number | null;
  /** contracts filled now by walking the book */
  fillQty: number;
  /** contracts left resting at the limit (limit orders below / beyond the book) */
  restQty: number;
  /** total contracts (fill + rest) */
  qty: number;
  /** blended average entry, cents */
  avgCents: number | null;
  /** USD spent on contracts (fees excluded) */
  costUsd: number;
  /** null = fee unknown ([FEE]) */
  feeUsd: number | null;
  /** cost + fee (fee ignored when unknown) */
  maxLossUsd: number;
  /** $1 per contract if the side wins */
  payoutUsd: number;
  /** payout - cost - fee */
  profitUsd: number;
  /** side fair - avg entry, cents */
  edgeCents: number | null;
  /** marketable order the book cannot fill completely within the cap (the rest is cancelled, IOC) */
  partial: boolean;
  /** estimate used depth (false = top of book only) */
  depth: boolean;
}

/**
 * Walk the selected side's book up to the order's limit to estimate the fill. Marketable = IOC at
 * ask + maxSlippage (unfilled remainder is dropped); limit = fills what crosses, the rest rests at the limit.
 */
export function estimateTicket(i: EstimateInput): TicketEstimate | null {
  const q = sideQuote(i.odds, i.side);
  const book = i.book && (i.book.bids.length || i.book.asks.length) ? sideBook(i.book, i.side) : null;
  const ask = book?.asks[0]?.price ?? q.ask;
  const bid = book?.bids[0]?.price ?? q.bid;
  if (ask == null && !(i.type === "limit" && i.limitCents != null)) return null;
  const usd = i.usd != null && i.usd > 0 ? i.usd : 0;
  const slip = Math.max(0, i.maxSlippageCents ?? 0);
  const limit =
    i.type === "marketable"
      ? Math.min(MAX_PRICE_CENTS, (ask as number) + slip)
      : Math.min(MAX_PRICE_CENTS, Math.max(MIN_PRICE_CENTS, i.limitCents ?? (ask as number)));
  const levels: BookLevel[] = book?.asks.length ? book.asks : ask != null ? [{ price: ask, size: 1e9 }] : [];
  let fillQty: number;
  let cost: number;
  let restQty = 0;
  let complete = true;
  if (i.type === "marketable") {
    // a USD budget walked up to ask + slippage, IOC (the backend's marketable buy)
    const sim = simulateFill({ action: "buy", levels, usd, limitCents: limit, feeCentsPerContract: null });
    fillQty = sim.qty;
    cost = sim.notionalUsd;
    complete = sim.complete;
  } else {
    // limit: usd / limit contracts; the part the book crosses fills at the ask levels, the rest rests at the limit
    const want = floorQty(usd / (limit / 100));
    const w = walkQty(levels, want, limit);
    fillQty = w.qty;
    cost = w.notionalUsd;
    restQty = floorQty(want - w.qty);
    cost += (restQty * limit) / 100;
  }
  fillQty = round1(fillQty);
  restQty = round1(restQty);
  const qty = round1(fillQty + restQty);
  cost = round2(cost);
  const avg = qty > 0 ? (cost * 100) / qty : null;
  const fee = i.feeCentsPerContract == null ? null : round2((i.feeCentsPerContract * qty) / 100);
  const payout = round2(qty);
  const sideFair = i.fairUp == null ? null : i.side === "up" ? i.fairUp : 100 - i.fairUp;
  return {
    ask,
    bid,
    limitCents: limit,
    fillQty,
    restQty,
    qty,
    avgCents: avg,
    costUsd: cost,
    feeUsd: fee,
    maxLossUsd: round2(cost + (fee ?? 0)),
    payoutUsd: payout,
    profitUsd: round2(payout - cost - (fee ?? 0)),
    edgeCents: sideFair != null && avg != null ? sideFair - avg : null,
    partial: i.type === "marketable" && usd > 0 && !complete,
    depth: !!book?.asks.length,
  };
}

export type TicketField = "usd" | "limitCents" | "maxSlippageCents" | "tpCents" | "slCents" | "btcStop" | "timeExitSec";
export type TicketErrors = Partial<Record<TicketField, string>>;

export interface ValidateContext {
  est: TicketEstimate | null;
  /** current BRTI proxy, USD */
  btc: number | null;
  /** seconds left in the current window */
  secLeft: number | null;
  limits: RiskLimits | null;
}

/** Field errors for the draft (empty object = submittable). Messages are short, user-facing sentences. */
export function validateTicket(d: TicketDraft, c: ValidateContext): TicketErrors {
  const e: TicketErrors = {};
  const usd = parseNum(d.usd);
  if (usd == null || usd <= 0) e.usd = "Enter an amount above $0.";
  else if (usd < 1) e.usd = "Minimum order is $1.";
  else if (c.limits && usd > c.limits.maxPositionUsd) e.usd = `Above max position $${c.limits.maxPositionUsd}.`;

  if (d.type === "limit" && d.limitCents.trim() !== "") {
    const l = parseNum(d.limitCents);
    if (l == null || l < MIN_PRICE_CENTS || l > MAX_PRICE_CENTS) e.limitCents = `Limit must be ${MIN_PRICE_CENTS}–${MAX_PRICE_CENTS}¢.`;
  }
  const slip = parseNum(d.maxSlippageCents);
  if (slip == null || slip < 0 || slip > 50) e.maxSlippageCents = "Slippage must be 0–50¢.";

  if (d.bracketOn)
    Object.assign(
      e,
      validateBracket(d, d.side, { entry: c.est?.avgCents ?? c.est?.ask ?? null, bid: c.est?.bid ?? null, btc: c.btc, secLeft: c.secLeft }),
    );
  return e;
}

const fmtC = (v: number) => `${round1(v)}¢`;

/** The bracket legs of a draft (ticket) or of the Edit bracket form. */
export type BracketFields = Pick<
  TicketDraft,
  "tpOn" | "tpCents" | "slOn" | "slCents" | "btcStopOn" | "btcStop" | "timeExitOn" | "timeExitSec"
>;

export interface BracketContext {
  /** entry price, cents: TP must be above / SL below it (null = no entry check, e.g. editing a live position) */
  entry: number | null;
  /** current bid of the side, cents: legs that would trigger at once are rejected */
  bid: number | null;
  btc: number | null;
  secLeft: number | null;
}

/** Errors for the enabled bracket legs. Mirrors the watcher: TP/SL compare the side's BID, BTC stop the proxy. */
export function validateBracket(f: BracketFields, side: Side, c: BracketContext): TicketErrors {
  const e: TicketErrors = {};
  const tp = f.tpOn ? parseNum(f.tpCents) : null;
  const sl = f.slOn ? parseNum(f.slCents) : null;
  if (f.tpOn) {
    if (tp == null || tp < MIN_PRICE_CENTS || tp > MAX_PRICE_CENTS) e.tpCents = `Take profit must be ${MIN_PRICE_CENTS}–${MAX_PRICE_CENTS}¢.`;
    else if (c.entry != null && tp <= c.entry) e.tpCents = `Take profit must be above the entry (${fmtC(c.entry)}).`;
    else if (c.bid != null && tp <= c.bid) e.tpCents = `At or below the bid (${fmtC(c.bid)}): it would exit at once.`;
  }
  if (f.slOn) {
    if (sl == null || sl < MIN_PRICE_CENTS || sl > MAX_PRICE_CENTS) e.slCents = `Stop loss must be ${MIN_PRICE_CENTS}–${MAX_PRICE_CENTS}¢.`;
    else if (c.entry != null && sl >= c.entry) e.slCents = `Stop loss must be below the entry (${fmtC(c.entry)}).`;
    else if (c.bid != null && sl >= c.bid) e.slCents = `At or above the bid (${fmtC(c.bid)}): it would exit at once.`;
  }
  if (tp != null && sl != null && !e.tpCents && !e.slCents && tp <= sl) e.tpCents = "Take profit must be above the stop loss.";
  if (f.btcStopOn) {
    const s = parseNum(f.btcStop);
    if (s == null || s <= 0) e.btcStop = "Enter a BTC price.";
    else if (c.btc != null && (side === "up" ? c.btc < s : c.btc > s))
      e.btcStop = `BTC is already ${side === "up" ? "below" : "above"} this: it would exit at once.`;
  }
  if (f.timeExitOn) {
    const t = parseNum(f.timeExitSec);
    if (t == null || t < MIN_TIME_EXIT_SEC || t >= WINDOW_SEC || !Number.isInteger(t))
      e.timeExitSec = `Whole seconds, ${MIN_TIME_EXIT_SEC}–${WINDOW_SEC - 1}.`;
    else if (c.secLeft != null && c.secLeft <= t) e.timeExitSec = "Less time left in the window: it would exit at once.";
  }
  return e;
}

/** BracketInput from bracket fields (null when every leg is off). */
export function bracketFromFields(f: BracketFields, maxSlippageCents?: number | null): BracketInput | null {
  const b: BracketInput = {
    tpCents: f.tpOn ? parseNum(f.tpCents) : null,
    slCents: f.slOn ? parseNum(f.slCents) : null,
    btcStop: f.btcStopOn ? parseNum(f.btcStop) : null,
    timeExitSec: f.timeExitOn ? parseNum(f.timeExitSec) : null,
    maxSlippageCents: maxSlippageCents ?? null,
  };
  return b.tpCents == null && b.slCents == null && b.btcStop == null && b.timeExitSec == null ? null : b;
}

/** Bracket fields from a stored bracket (Edit bracket form). Off legs keep a blank value. */
export function fieldsFromBracket(b: BracketInput | null): BracketFields {
  const t = (v: number | null | undefined) => (v == null ? "" : String(v));
  return {
    tpOn: b?.tpCents != null,
    tpCents: t(b?.tpCents),
    slOn: b?.slCents != null,
    slCents: t(b?.slCents),
    btcStopOn: b?.btcStop != null,
    btcStop: t(b?.btcStop),
    timeExitOn: b?.timeExitSec != null,
    timeExitSec: b?.timeExitSec == null ? "30" : String(b.timeExitSec),
  };
}

/** BracketInput from the draft (null when the bracket is off or every leg is off). */
export function bracketFromDraft(d: TicketDraft, maxSlippageCents?: number | null): BracketInput | null {
  return d.bracketOn ? bracketFromFields(d, maxSlippageCents) : null;
}

/** POST /api/orders body from a validated draft. */
export function buildOrderRequest(
  d: TicketDraft,
  opts: { liveSession?: string | null; clientOrderId?: string | null; fallbackLimitCents?: number | null } = {},
): PlaceOrderRequest {
  const slip = parseNum(d.maxSlippageCents) ?? 0;
  // a limit order with an empty limit field joins the current ask (the server needs a price)
  const limit = d.type === "limit" ? parseNum(d.limitCents) ?? opts.fallbackLimitCents ?? null : null;
  return {
    side: d.side,
    type: d.type,
    usd: parseNum(d.usd) ?? 0,
    limitCents: limit,
    maxSlippageCents: slip,
    bracket: bracketFromDraft(d, slip),
    liveSession: opts.liveSession ?? null,
    clientOrderId: opts.clientOrderId ?? null,
  };
}

/**
 * Starting values when the bracket is switched on: TP / SL a quarter of the way to 99¢ / 1¢ from the entry
 * (at least 3¢ away, whole cents), BTC stop at the price to beat, time exit 30s. With the side's bid known, the
 * SL is kept below it and the TP above it (validateBracket would reject them otherwise); when there is no room
 * (bid at 1¢ / 99¢) that leg is left blank.
 */
export function suggestBracket(
  entryCents: number | null,
  ptb: number | null,
  bid: number | null = null,
): Pick<TicketDraft, "tpCents" | "slCents" | "btcStop" | "timeExitSec"> {
  const e = entryCents == null ? null : Math.round(entryCents);
  let tpN = e == null ? null : Math.min(MAX_PRICE_CENTS, e + Math.max(3, Math.round((MAX_PRICE_CENTS - e) / 4)));
  let slN = e == null ? null : Math.max(MIN_PRICE_CENTS, e - Math.max(3, Math.round((e - MIN_PRICE_CENTS) / 4)));
  if (bid != null) {
    if (slN != null && slN >= bid) slN = Math.ceil(bid) - 1 >= MIN_PRICE_CENTS ? Math.ceil(bid) - 1 : null;
    if (tpN != null && tpN <= bid) tpN = Math.floor(bid) + 1 <= MAX_PRICE_CENTS ? Math.floor(bid) + 1 : null;
  }
  const tp = tpN == null ? "" : String(tpN);
  const sl = slN == null ? "" : String(slN);
  return { tpCents: tp, slCents: sl, btcStop: ptb == null ? "" : String(Math.round(ptb)), timeExitSec: "30" };
}

/** Window + side the ticket's TP / SL / BTC stop were suggested for. */
export interface BracketBasis {
  windowId: string;
  side: Side;
}

/**
 * Fresh TP / SL / BTC stop when the draft's bracket values were suggested for another window or side (or after a
 * submit, basis null): a stop at the previous window's price to beat, or Up-priced TP / SL on a Down entry, must
 * not be armed silently. null = the basis still matches, keep the values. The returned basis is null while there
 * is no entry quote yet, so the TP / SL are suggested again once one arrives.
 */
export function resuggestBracket(
  basis: BracketBasis | null,
  now: { windowId: string | null; side: Side; entryCents: number | null; ptb: number | null; bid?: number | null },
): { patch: Pick<TicketDraft, "tpCents" | "slCents" | "btcStop">; basis: BracketBasis | null } | null {
  if (basis && basis.windowId === now.windowId && basis.side === now.side) return null;
  const s = suggestBracket(now.entryCents, now.ptb, now.bid ?? null);
  return {
    patch: { tpCents: s.tpCents, slCents: s.slCents, btcStop: s.btcStop },
    basis: now.entryCents != null && now.windowId ? { windowId: now.windowId, side: now.side } : null,
  };
}

export type BarTone = "ok" | "warn" | "danger";

/** Progress bar fill 0-100 and tone (warn >= 60%, danger >= 85%). */
export function riskBar(used: number, limit: number): { pct: number; tone: BarTone } {
  const pct = limit > 0 ? Math.min(100, Math.max(0, (used / limit) * 100)) : used > 0 ? 100 : 0;
  return { pct, tone: pct >= 85 ? "danger" : pct >= 60 ? "warn" : "ok" };
}

/** Why new orders cannot be placed right now (null = they can). Order of checks = what the user fixes first. */
export function blockReason(c: {
  replayActive: boolean;
  hasWindow: boolean;
  hasQuote: boolean;
  risk: Pick<RiskState, "killSwitch" | "tradingDisabledUntil" | "disabledReason"> | null;
  now: number;
}): string | null {
  if (c.replayActive) return "Exit replay to trade.";
  if (c.risk?.killSwitch) return "Kill switch engaged. Re-arm trading in Risk.";
  if (c.risk?.tradingDisabledUntil != null && c.risk.tradingDisabledUntil > c.now) return c.risk.disabledReason ?? "Trading disabled.";
  if (!c.hasWindow) return "No live market window.";
  if (!c.hasQuote) return "Waiting for a quote.";
  return null;
}
