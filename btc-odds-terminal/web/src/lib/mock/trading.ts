// Mock paper trading for mock mode (NEXT_PUBLIC_MOCK=1 / ?mock=1). OWNER: trading-backend (written by trading-ui).
//
// A complete MockTrading (orders, fills, positions, OCO brackets, settlement, kill support) mirroring
// backend/src/trading semantics, so the right panel is fully demoable without a backend:
//   - marketable buy = USD budget walked up to ask + maxSlippage (IOC); limit buy = usd / limit contracts, the
//     crossing part fills at the ask levels, the rest rests and fills at the limit when the book crosses it;
//   - exits (close / TP / SL / BTC stop / time exit / kill) = marketable sell at bid - slippage (IOC);
//   - brackets are OCO (first trigger wins, evaluateBracket priority), checked every simulated second;
//   - at rollover the settled window's positions pay 100¢ / 0, its resting orders are cancelled;
//   - live mode orders need a confirmed mock live session (409 live_confirm_required).
//   - bracket legs: TP must stay above / SL below the side's bid (else 400), time exit >= 3s (backend rules);
//   - the kill switch's flatten arms a triggered `kill` bracket for whatever it could not sell, retried every second.

import type {
  Bracket,
  BracketInput,
  ClosePositionRequest,
  Fill,
  MarketWindow,
  Notice,
  Order,
  OrderReason,
  PlaceOrderRequest,
  PlaceOrderResponse,
  Position,
  Side,
} from "@shared/protocol";
import { bracketEmpty, evaluateBracket, sideBook, simulateFill } from "@shared/paper";
import { etDay } from "@shared/risk";
import { ApiError } from "../api-error";
import type { MockCtx, MockRequest, MockTrading } from "./types";

const EPS = 1e-9;
const LIST_CAP = 200;
const DEFAULT_EXIT_SLIP = 3;
/** backend MIN_TIME_EXIT_SEC: the watcher checks once a second and the exit needs time to fill */
export const MIN_TIME_EXIT_SEC = 3;
/** backend FLATTEN_EXIT_SLIPPAGE_CENTS: kill retries sell with at least this cap */
const FLATTEN_EXIT_SLIP = 10;
const REASON_LABEL: Record<OrderReason, string> = {
  manual: "Order",
  close: "Close",
  tp: "Take profit",
  sl: "Stop loss",
  btc_stop: "BTC stop",
  time_exit: "Time exit",
  kill: "Kill switch",
};

const r1 = (v: number) => Math.round(v * 10) / 10;
const r2 = (v: number) => Math.round(v * 100) / 100;
const r4 = (v: number) => Math.round(v * 10000) / 10000;
const floorQty = (q: number) => (q > 0 ? Math.floor(q * 10 + 1e-7) / 10 : 0);
const clampPx = (c: number) => Math.min(99.9, Math.max(0.1, Math.round(c * 10) / 10));
const sideName = (s: Side) => (s === "up" ? "Up" : "Down");
const bad = (msg: string) => new ApiError(400, "validation", msg);

export function createMockTrading(ctx: MockCtx): MockTrading {
  let seq = 0;
  const id = (p: string) => `${p}-${Date.now().toString(36)}-${(++seq).toString(36)}`;
  const orders = new Map<string, Order>();
  const fills: Fill[] = []; // newest first
  const positions = new Map<string, Position>();
  const brackets = new Map<string, Bracket>();
  /** bracket requested on a resting entry order, armed on its first fill */
  const pendingBracket = new Map<string, BracketInput>();
  /** realized P&L events (ts, usd) for the ET-day total */
  const realized: { ts: number; usd: number; windowId: string }[] = [];

  // ---- change batching (emit once per call) ----
  let chOrders = new Map<string, Order>();
  let chFills: Fill[] = [];
  let chPos = new Map<string, Position>();
  let chBr = new Map<string, Bracket>();
  const touchO = (o: Order) => chOrders.set(o.id, o);
  const touchP = (p: Position) => chPos.set(p.id, p);
  const touchB = (b: Bracket) => chBr.set(b.id, b);
  const notice = (level: Notice["level"], title: string, message: string) =>
    ctx.emit({ type: "notice", data: { id: id("n"), ts: ctx.now(), level, title, message } });
  const flush = () => {
    if (chOrders.size) ctx.emit({ type: "orders", data: [...chOrders.values()].map((x) => ({ ...x })) });
    if (chFills.length) ctx.emit({ type: "fills", data: chFills.map((x) => ({ ...x })) });
    if (chPos.size) ctx.emit({ type: "positions", data: [...chPos.values()].map((x) => ({ ...x })) });
    if (chBr.size) ctx.emit({ type: "brackets", data: [...chBr.values()].map((x) => ({ ...x })) });
    const any = chOrders.size || chFills.length || chPos.size || chBr.size;
    chOrders = new Map();
    chFills = [];
    chPos = new Map();
    chBr = new Map();
    if (any) ctx.modules.risk.refresh();
  };

  const fee = () => ctx.settings().feeCentsPerContract;
  const liveWindow = (): MarketWindow | null => {
    const w = ctx.current();
    const now = ctx.now();
    return w && w.status === "live" && now >= w.startTs && now < w.endTs ? w : null;
  };
  const curBook = (w: MarketWindow) => {
    const b = ctx.book();
    return b && b.windowId === w.id ? b : null;
  };
  const openPos = (mode: Order["mode"], windowId: string, side: Side) => {
    for (const p of positions.values()) if (p.status === "open" && p.mode === mode && p.windowId === windowId && p.side === side) return p;
    return null;
  };
  const activeBracket = (positionId: string) => {
    for (const b of brackets.values()) if (b.positionId === positionId && b.status === "active") return b;
    return null;
  };
  const triggeredBracket = (positionId: string) => {
    for (const b of brackets.values()) if (b.positionId === positionId && b.status === "triggered") return b;
    return null;
  };
  /** the position is flat / settled: a triggered bracket becomes `triggeredTo`, an active one is cancelled */
  const finishBrackets = (p: Position, triggeredTo: "done" | "cancelled") => {
    for (const b of brackets.values()) {
      if (b.positionId !== p.id || (b.status !== "active" && b.status !== "triggered")) continue;
      b.status = b.status === "triggered" ? triggeredTo : "cancelled";
      b.updatedTs = ctx.now();
      touchB(b);
    }
  };
  const working = () => [...orders.values()].filter((o) => o.status === "open" || o.status === "partial" || o.status === "pending");
  const restingBuyUsd = (o: Order) => (Math.max(0, o.qty - o.filledQty) * (o.limitCents ?? 0)) / 100;
  const windowCost = (windowId: string, mode: Order["mode"]) => {
    let c = 0;
    for (const p of positions.values()) if (p.status === "open" && p.windowId === windowId && p.mode === mode) c += p.costUsd;
    for (const o of working()) if (o.action === "buy" && o.windowId === windowId && o.mode === mode) c += restingBuyUsd(o);
    for (const e of realized) if (e.windowId === windowId && e.usd < 0) c += -e.usd;
    return c;
  };

  const markOf = (p: Position): number | null => {
    const w = ctx.current();
    if (p.windowId !== w.id) return p.markCents;
    const b = curBook(w);
    if (b) return sideBook(b, p.side).bids[0]?.price ?? null;
    const o = ctx.lastOdds();
    if (!o || o.windowId !== w.id) return null;
    return p.side === "up" ? o.upBid : o.downBid;
  };
  const remark = (p: Position, force = false) => {
    if (p.status !== "open") return;
    const m = markOf(p);
    const u = m == null ? null : r2((p.qty * (m - p.avgCents)) / 100);
    if (force || m !== p.markCents || u !== p.unrealizedPnlUsd) {
      p.markCents = m;
      p.unrealizedPnlUsd = u;
      p.updatedTs = ctx.now();
      touchP(p);
    }
  };

  /** record a buy fill: updates order + position (creating it), arms a pending bracket */
  const buyFill = (o: Order, qty: number, avgCents: number, refCents: number | null) => {
    const now = ctx.now();
    const f = fee();
    const feeUsd = f == null ? null : r2((f * qty) / 100);
    const fill: Fill = {
      id: id("f"),
      orderId: o.id,
      externalId: null,
      mode: o.mode,
      windowId: o.windowId,
      instrument: o.instrument,
      side: o.side,
      action: "buy",
      priceCents: r4(avgCents),
      qty,
      feeUsd,
      slippageCents: refCents == null ? null : r4(avgCents - refCents),
      ts: now,
    };
    fills.unshift(fill);
    chFills.push(fill);
    const prevFilled = o.filledQty;
    o.filledQty = r1(o.filledQty + qty);
    o.avgFillCents = r4(((o.avgFillCents ?? 0) * prevFilled + avgCents * qty) / o.filledQty);
    o.feesUsd = r2(o.feesUsd + (feeUsd ?? 0));
    o.updatedTs = now;
    let p = openPos(o.mode, o.windowId, o.side);
    if (!p) {
      p = {
        id: id("pos"),
        mode: o.mode,
        windowId: o.windowId,
        instrument: o.instrument,
        side: o.side,
        qty: 0,
        avgCents: 0,
        costUsd: 0,
        realizedPnlUsd: 0,
        feesUsd: 0,
        status: "open",
        markCents: null,
        unrealizedPnlUsd: null,
        bracketId: null,
        openedTs: now,
        updatedTs: now,
        closedTs: null,
      };
      positions.set(p.id, p);
    }
    const q = r1(p.qty + qty);
    p.avgCents = r4((p.avgCents * p.qty + avgCents * qty) / q);
    p.qty = q;
    p.costUsd = r2((p.qty * p.avgCents) / 100);
    p.feesUsd = r2(p.feesUsd + (feeUsd ?? 0));
    // entry fees are realized at once (backend accounting): realized P&L and the daily loss include them
    if (feeUsd) {
      p.realizedPnlUsd = r2(p.realizedPnlUsd - feeUsd);
      realized.push({ ts: now, usd: -feeUsd, windowId: o.windowId });
    }
    o.positionId = p.id;
    remark(p, true);
    const pb = pendingBracket.get(o.id);
    if (pb) {
      pendingBracket.delete(o.id);
      setBracket(p, pb);
    }
    touchO(o);
  };

  /** create / replace / remove (null) the active bracket of a position */
  const setBracket = (p: Position, input: BracketInput | null): Bracket | null => {
    const now = ctx.now();
    const old = activeBracket(p.id);
    if (bracketEmpty(input)) {
      if (old) {
        old.status = "cancelled";
        old.updatedTs = now;
        touchB(old);
      }
      p.bracketId = null;
      p.updatedTs = now;
      touchP(p);
      return null;
    }
    const b: Bracket = old ?? {
      id: id("br"),
      positionId: p.id,
      windowId: p.windowId,
      tpCents: null,
      slCents: null,
      btcStop: null,
      timeExitSec: null,
      maxSlippageCents: null,
      status: "active",
      triggerReason: null,
      triggeredTs: null,
      exitOrderId: null,
      createdTs: now,
      updatedTs: now,
    };
    b.tpCents = input!.tpCents ?? null;
    b.slCents = input!.slCents ?? null;
    b.btcStop = input!.btcStop ?? null;
    b.timeExitSec = input!.timeExitSec ?? null;
    b.maxSlippageCents = input!.maxSlippageCents ?? b.maxSlippageCents ?? null;
    b.updatedTs = now;
    brackets.set(b.id, b);
    p.bracketId = b.id;
    p.updatedTs = now;
    touchB(b);
    touchP(p);
    return b;
  };

  /** marketable IOC sell of the whole position at bid - slippage (paper) */
  const exit = (p: Position, reason: OrderReason, slipIn: number | null, bracketId: string | null): Order => {
    const w = ctx.current();
    if (p.status !== "open") throw bad(`position is ${p.status}`);
    if (w.id !== p.windowId || !liveWindow()) throw new ApiError(409, "no_market", "this window has closed; the position settles at expiry");
    const book = curBook(w);
    const bids = book ? sideBook(book, p.side).bids : [];
    const refBid = bids[0]?.price ?? null;
    if (refBid == null) throw new ApiError(409, "no_market", `no ${sideName(p.side)} bid to sell into`);
    const b = bracketId ? brackets.get(bracketId) ?? null : activeBracket(p.id);
    const slip = slipIn ?? b?.maxSlippageCents ?? DEFAULT_EXIT_SLIP;
    const now = ctx.now();
    const limit = clampPx(refBid - slip);
    const sim = simulateFill({ action: "sell", levels: bids, qty: p.qty, limitCents: limit, feeCentsPerContract: fee() });
    const o: Order = {
      id: id("ord"),
      clientOrderId: null,
      externalId: null,
      mode: p.mode,
      windowId: p.windowId,
      instrument: p.instrument,
      side: p.side,
      action: "sell",
      type: "marketable",
      limitCents: limit,
      maxSlippageCents: slip,
      qty: p.qty,
      notionalUsd: null,
      filledQty: sim.qty,
      avgFillCents: sim.avgCents == null ? null : r4(sim.avgCents),
      feesUsd: sim.feeUsd ?? 0,
      status: sim.qty <= 0 ? "cancelled" : sim.complete ? "filled" : "cancelled",
      reason,
      bracketId,
      positionId: p.id,
      error: sim.qty <= 0 ? "nothing to sell into within the slippage cap" : null,
      createdTs: now,
      updatedTs: now,
    };
    orders.set(o.id, o);
    touchO(o);
    if (sim.qty > 0 && sim.avgCents != null) {
      const fill: Fill = {
        id: id("f"),
        orderId: o.id,
        externalId: null,
        mode: o.mode,
        windowId: o.windowId,
        instrument: o.instrument,
        side: o.side,
        action: "sell",
        priceCents: r4(sim.avgCents),
        qty: sim.qty,
        feeUsd: sim.feeUsd,
        slippageCents: r4(refBid - sim.avgCents),
        ts: now,
      };
      fills.unshift(fill);
      chFills.push(fill);
      const pnl = r2((sim.qty * (sim.avgCents - p.avgCents)) / 100 - (sim.feeUsd ?? 0));
      realized.push({ ts: now, usd: pnl, windowId: p.windowId });
      p.realizedPnlUsd = r2(p.realizedPnlUsd + pnl);
      p.feesUsd = r2(p.feesUsd + (sim.feeUsd ?? 0));
      p.qty = r1(p.qty - sim.qty);
      p.costUsd = r2((p.qty * p.avgCents) / 100);
      p.updatedTs = now;
      if (p.qty <= EPS) {
        p.qty = 0;
        p.costUsd = 0;
        p.status = "closed";
        p.closedTs = now;
        p.unrealizedPnlUsd = 0;
        finishBrackets(p, "done");
        p.bracketId = null;
      } else remark(p, true);
      touchP(p);
    }
    return o;
  };

  const response = (o: Order): PlaceOrderResponse => {
    const p = o.positionId ? positions.get(o.positionId) ?? null : null;
    const b = p ? activeBracket(p.id) : null;
    return {
      order: { ...o },
      fills: fills.filter((f) => f.orderId === o.id).map((f) => ({ ...f })),
      position: p ? { ...p } : null,
      bracket: b ? { ...b } : null,
    };
  };

  const requireLive = (mode: Order["mode"], session: string | null | undefined) => {
    if (mode === "live" && !ctx.modules.live.isSessionConfirmed(session)) {
      throw new ApiError(409, "live_confirm_required", "confirm live trading for this session first");
    }
  };

  // ---- routes ----
  const place = ({ body }: MockRequest): PlaceOrderResponse => {
    const req = (body ?? {}) as Partial<PlaceOrderRequest>;
    // idempotent retries (backend: orderByClientId): the same key returns the order it already placed
    if (req.clientOrderId) {
      for (const o of orders.values()) if (o.clientOrderId === req.clientOrderId) return response(o);
    }
    if (req.side !== "up" && req.side !== "down") throw bad("side must be up or down");
    if (req.type !== "limit" && req.type !== "marketable") throw bad("type must be limit or marketable");
    if (typeof req.usd !== "number" || !(req.usd > 0)) throw bad("usd must be > 0");
    const slip = req.maxSlippageCents;
    if (typeof slip !== "number" || slip < 0 || slip > 99) throw bad("maxSlippageCents must be 0-99");
    if (req.type === "limit" && (typeof req.limitCents !== "number" || req.limitCents <= 0 || req.limitCents >= 100))
      throw bad("limitCents must be 0-100 for a limit order");
    if (req.bracket) checkLegs(req.bracket);
    const mode = ctx.settings().mode;
    const w = liveWindow();
    if (!w) throw new ApiError(409, "no_market", "no live market window right now");
    requireLive(mode, req.liveSession);
    const book = curBook(w);
    const asks = book ? sideBook(book, req.side).asks : [];
    const refAsk = asks[0]?.price ?? null;
    let limit: number;
    let qty: number;
    let addCost: number;
    if (req.type === "marketable") {
      if (refAsk == null) throw new ApiError(409, "no_market", `no ${sideName(req.side)} ask to buy from`);
      limit = clampPx(refAsk + slip);
      qty = floorQty(req.usd / (limit / 100));
      addCost = req.usd;
    } else {
      limit = clampPx(req.limitCents!);
      qty = floorQty(req.usd / (limit / 100));
      addCost = r2((qty * limit) / 100);
    }
    if (!(qty > 0)) throw bad(`$${req.usd} buys less than 0.1 contract at ${limit}¢`);
    const existing = openPos(mode, w.id, req.side);
    const now = ctx.now();
    const o: Order = {
      id: id("ord"),
      clientOrderId: req.clientOrderId ?? null,
      externalId: null,
      mode,
      windowId: w.id,
      instrument: req.side === "up" ? w.upInstrument : w.downInstrument,
      side: req.side,
      action: "buy",
      type: req.type,
      limitCents: limit,
      maxSlippageCents: slip,
      qty,
      notionalUsd: req.usd,
      filledQty: 0,
      avgFillCents: null,
      feesUsd: 0,
      status: "pending",
      reason: "manual",
      bracketId: null,
      positionId: existing?.id ?? null,
      error: null,
      createdTs: now,
      updatedTs: now,
    };
    const check = ctx.modules.risk.check({
      addCostUsd: addCost,
      windowCostUsd: windowCost(w.id, mode),
      positionCostUsd: existing?.costUsd ?? 0,
      reducing: false,
    });
    orders.set(o.id, o);
    if (!check.ok) {
      o.status = "rejected";
      o.error = check.reason;
      touchO(o);
      flush();
      throw new ApiError(check.code === "trading_disabled" ? 423 : 403, check.code, check.reason);
    }
    const bracketIn = bracketEmpty(req.bracket) ? null : { ...req.bracket!, maxSlippageCents: req.bracket!.maxSlippageCents ?? slip };
    if (bracketIn) pendingBracket.set(o.id, bracketIn);
    if (req.type === "marketable") {
      const sim = simulateFill({ action: "buy", levels: asks, usd: req.usd, limitCents: limit, feeCentsPerContract: null });
      if (sim.qty > 0 && sim.avgCents != null) buyFill(o, sim.qty, sim.avgCents, refAsk);
      o.qty = sim.complete ? o.filledQty : o.qty;
      o.status = sim.complete && sim.qty > 0 ? "filled" : "cancelled";
      if (o.status === "cancelled") {
        o.error = sim.qty > 0 ? "rest beyond the slippage cap (IOC)" : "nothing to buy within the slippage cap";
        pendingBracket.delete(o.id);
      }
    } else {
      // the crossing part fills now at the ask levels, the remainder rests at the limit
      let left = qty;
      let notional = 0;
      let got = 0;
      for (const l of asks) {
        if (left <= EPS || l.price > limit + EPS) break;
        const take = Math.min(left, floorQty(l.size));
        if (take <= 0) continue;
        got = r1(got + take);
        notional += (take * l.price) / 100;
        left = r1(left - take);
      }
      if (got > 0) buyFill(o, got, (notional * 100) / got, refAsk);
      o.status = left <= EPS ? "filled" : got > 0 ? "partial" : "open";
    }
    o.updatedTs = ctx.now();
    touchO(o);
    flush();
    if (o.status === "cancelled" && o.filledQty <= 0) throw new ApiError(409, "no_market", o.error ?? "nothing filled");
    return response(o);
  };

  const cancel = ({ params }: MockRequest): Order => {
    const o = orders.get(params.id);
    if (!o) throw new ApiError(404, "not_found", "order not found");
    if (!(o.status === "open" || o.status === "partial" || o.status === "pending")) throw bad(`order is ${o.status}`);
    o.status = "cancelled";
    o.updatedTs = ctx.now();
    pendingBracket.delete(o.id);
    touchO(o);
    flush();
    return { ...o };
  };

  const close = ({ params, body }: MockRequest): PlaceOrderResponse => {
    const p = positions.get(params.id);
    if (!p) throw new ApiError(404, "not_found", "position not found");
    const req = (body ?? {}) as ClosePositionRequest;
    requireLive(p.mode, req.liveSession);
    const o = exit(p, "close", req.maxSlippageCents ?? null, null);
    flush();
    if (o.filledQty <= 0) throw new ApiError(409, "no_market", o.error ?? "nothing filled");
    return response(o);
  };

  /** leg shapes (backend validate.ts parseBracket): prices 0-100, TP above SL, BTC stop > 0, time exit >= 3s */
  const checkLegs = (input: BracketInput) => {
    for (const k of ["tpCents", "slCents"] as const) {
      const v = input[k];
      if (v != null && (typeof v !== "number" || v <= 0 || v >= 100)) throw bad(`${k} must be 0-100`);
    }
    if (input.btcStop != null && !(input.btcStop > 0)) throw bad("btcStop must be > 0");
    if (input.timeExitSec != null && !(input.timeExitSec >= MIN_TIME_EXIT_SEC))
      throw bad(`time exit must be at least ${MIN_TIME_EXIT_SEC} s before the window ends; turn it off with null`);
    if (input.tpCents != null && input.slCents != null && input.tpCents <= input.slCents) throw bad("take profit must be above stop loss");
  };

  const putBracket = ({ params, body }: MockRequest): Bracket | null => {
    const p = positions.get(params.id);
    if (!p) throw new ApiError(404, "not_found", "position not found");
    if (p.status !== "open") throw bad(`position is ${p.status}`);
    if (triggeredBracket(p.id)) throw bad("the bracket already triggered; its exit is working");
    const input = (body ?? null) as BracketInput | null;
    if (input) {
      checkLegs(input);
      const w = ctx.current();
      const book = w.id === p.windowId ? curBook(w) : null;
      const bid = book ? sideBook(book, p.side).bids[0]?.price ?? null : null;
      if (bid != null && input.tpCents != null && input.tpCents <= bid)
        throw bad(`tpCents: ${input.tpCents}¢ is at or below the ${sideName(p.side)} bid (${bid}¢) and would trigger immediately; use Close to exit now`);
      if (bid != null && input.slCents != null && input.slCents >= bid)
        throw bad(`slCents: ${input.slCents}¢ is at or above the ${sideName(p.side)} bid (${bid}¢) and would trigger immediately; use Close to exit now`);
    }
    const b = setBracket(p, input);
    flush();
    return b ? { ...b } : null;
  };

  const newestFirst = <T extends { createdTs?: number; ts?: number; openedTs?: number }>(xs: T[], key: (x: T) => number) =>
    xs.sort((a, b) => key(b) - key(a));

  // ---- watcher ----
  /** a triggered bracket whose exit did not flatten the position sells the rest (backend: triggered retries) */
  const retryTriggered = (b: Bracket, book: ReturnType<typeof curBook>) => {
    const p = positions.get(b.positionId);
    if (!p || p.status !== "open" || p.qty <= EPS) {
      if (p) finishBrackets(p, "done");
      else {
        b.status = "done";
        b.updatedTs = ctx.now();
        touchB(b);
      }
      return;
    }
    if (!book || !sideBook(book, p.side).bids[0]) return; // nothing to sell into yet
    try {
      const o = exit(p, b.triggerReason ?? "close", b.maxSlippageCents, b.id);
      if (o.filledQty > 0) b.exitOrderId = o.id;
      if (p.status !== "open")
        notice("info", `${REASON_LABEL[b.triggerReason ?? "close"]} exit complete`, `Sold the rest of ${sideName(p.side)} @ ${(o.avgFillCents ?? 0).toFixed(1)}¢`);
    } catch {
      // window closing: the position settles at expiry (settleWindow cancels the bracket)
    }
  };

  const evaluate = (ts: number) => {
    const w = ctx.current();
    const book = curBook(w);
    // resting limit buys fill at the limit once the side's ask crosses it
    if (book) {
      for (const o of working()) {
        if (o.windowId !== w.id || o.action !== "buy" || o.limitCents == null) continue;
        const asks = sideBook(book, o.side).asks;
        if (!asks[0] || asks[0].price > o.limitCents + EPS) continue;
        let avail = 0;
        for (const l of asks) if (l.price <= o.limitCents + EPS) avail += l.size;
        const take = Math.min(r1(o.qty - o.filledQty), floorQty(avail));
        if (take <= 0) continue;
        buyFill(o, take, o.limitCents, asks[0].price);
        o.status = o.filledQty >= o.qty - EPS ? "filled" : "partial";
        if (o.status === "filled") notice("success", "Limit order filled", `Bought ${sideName(o.side)} ${o.filledQty.toFixed(1)} @ ${o.limitCents}¢`);
      }
    }
    for (const p of positions.values()) if (p.status === "open") remark(p);
    // OCO brackets
    const btc = ctx.lastTick()?.price ?? null;
    for (const b of [...brackets.values()]) {
      if (b.windowId !== w.id) continue;
      if (b.status === "triggered") {
        retryTriggered(b, book);
        continue;
      }
      if (b.status !== "active") continue;
      const p = positions.get(b.positionId);
      if (!p || p.status !== "open") continue;
      const bid = book ? sideBook(book, p.side).bids[0]?.price ?? null : null;
      const reason = evaluateBracket(b, p.side, { now: ts, sideBid: bid, btc, windowEndTs: w.endTs });
      if (!reason) continue;
      b.status = "triggered";
      b.triggerReason = reason;
      b.triggeredTs = ts;
      touchB(b);
      try {
        const o = exit(p, reason, b.maxSlippageCents, b.id);
        b.exitOrderId = o.id;
        if (o.filledQty > 0) {
          const pnl = r2((o.filledQty * ((o.avgFillCents ?? 0) - p.avgCents)) / 100 - o.feesUsd);
          // a partial IOC exit keeps the bracket `triggered`: retryTriggered sells the rest every second
          const rest = p.status === "open" && p.qty > EPS ? `; ${p.qty.toFixed(1)} left, retrying every second` : "";
          notice(
            reason === "tp" ? "success" : "warn",
            `${REASON_LABEL[reason]} hit`,
            `Sold ${sideName(p.side)} ${o.filledQty.toFixed(1)} @ ${(o.avgFillCents ?? 0).toFixed(1)}¢ (${pnl < 0 ? "−" : "+"}$${Math.abs(pnl).toFixed(2)})${rest}`,
          );
        } else {
          b.status = "active"; // retry next second
          notice("error", `${REASON_LABEL[reason]} exit failed`, `${o.error}. Retrying every second.`);
        }
      } catch (err) {
        b.status = "active";
        notice("error", `${REASON_LABEL[reason]} exit failed`, (err as Error).message);
      }
    }
    flush();
  };

  const settleWindow = (w: MarketWindow) => {
    const now = ctx.now();
    for (const o of working()) {
      if (o.windowId !== w.id) continue;
      o.status = "cancelled";
      o.error = "window closed";
      o.updatedTs = now;
      pendingBracket.delete(o.id);
      touchO(o);
    }
    for (const p of positions.values()) {
      if (p.windowId !== w.id || p.status !== "open") continue;
      const won = w.result != null && w.result === p.side;
      const px = won ? 100 : 0;
      const pnl = r2((p.qty * (px - p.avgCents)) / 100);
      realized.push({ ts: now, usd: pnl, windowId: p.windowId });
      p.realizedPnlUsd = r2(p.realizedPnlUsd + pnl);
      p.markCents = px;
      p.unrealizedPnlUsd = 0;
      p.status = "settled";
      p.closedTs = now;
      p.updatedTs = now;
      touchP(p);
      finishBrackets(p, "cancelled");
      p.bracketId = null;
      notice(won ? "success" : "info", `Settled ${won ? "in the money" : "out of the money"}`, `${sideName(p.side)} ${p.qty.toFixed(1)} @ ${p.avgCents.toFixed(1)}¢ → ${px}¢ (${pnl < 0 ? "−" : "+"}$${Math.abs(pnl).toFixed(2)})`);
    }
    flush();
  };

  const self: MockTrading = {
    name: "trading",
    routes: [
      {
        method: "GET",
        path: "/orders",
        handler: ({ query }) =>
          newestFirst(
            [...orders.values()].filter((o) => (!query.windowId || o.windowId === query.windowId) && (!query.status || o.status === query.status)),
            (o) => o.createdTs,
          ).slice(0, Number(query.limit) || LIST_CAP),
      },
      { method: "POST", path: "/orders", handler: place },
      { method: "DELETE", path: "/orders/:id", handler: cancel },
      {
        method: "GET",
        path: "/fills",
        handler: ({ query }) => fills.filter((f) => !query.windowId || f.windowId === query.windowId).slice(0, Number(query.limit) || LIST_CAP),
      },
      {
        method: "GET",
        path: "/positions",
        handler: ({ query }) =>
          newestFirst([...positions.values()].filter((p) => query.status !== "open" || p.status === "open"), (p) => p.openedTs).slice(
            0,
            Number(query.limit) || LIST_CAP,
          ),
      },
      { method: "POST", path: "/positions/:id/close", handler: close },
      { method: "PUT", path: "/positions/:id/bracket", handler: putBracket },
      {
        method: "GET",
        path: "/brackets",
        // status=active = still working (active, or triggered with its exit not flat yet), like the backend
        handler: ({ query }) => [...brackets.values()].filter((b) => query.status !== "active" || b.status === "active" || b.status === "triggered"),
      },
    ],
    positions: () => [...positions.values()].map((p) => ({ ...p })),
    fills: (windowId) => fills.filter((f) => !windowId || f.windowId === windowId).map((f) => ({ ...f })),
    riskInputs(windowId) {
      const mode = ctx.settings().mode;
      const day = etDay(ctx.now());
      let dailyRealizedPnlUsd = 0;
      for (const e of realized) if (etDay(e.ts) === day) dailyRealizedPnlUsd += e.usd;
      let unrealizedPnlUsd = 0;
      let openExposureUsd = 0;
      for (const p of positions.values()) {
        if (p.status !== "open" || p.mode !== mode) continue;
        unrealizedPnlUsd += p.unrealizedPnlUsd ?? 0;
        openExposureUsd += p.costUsd;
      }
      for (const o of working()) if (o.action === "buy" && o.mode === mode) openExposureUsd += restingBuyUsd(o);
      return {
        dailyRealizedPnlUsd: r2(dailyRealizedPnlUsd),
        unrealizedPnlUsd: r2(unrealizedPnlUsd),
        openExposureUsd: r2(openExposureUsd),
        windowLossUsedUsd: windowId ? r2(windowCost(windowId, mode)) : 0,
      };
    },
    cancelAll(reason) {
      let n = 0;
      for (const o of working()) {
        o.status = "cancelled";
        o.error = REASON_LABEL[reason];
        o.updatedTs = ctx.now();
        pendingBracket.delete(o.id);
        touchO(o);
        n++;
      }
      flush();
      return n;
    },
    flattenAll(reason) {
      let n = 0;
      const cur = ctx.current();
      for (const p of [...positions.values()]) {
        if (p.status !== "open") continue;
        const b = activeBracket(p.id);
        if (b) {
          b.status = "cancelled";
          b.updatedTs = ctx.now();
          touchB(b);
        }
        try {
          if (exit(p, reason, FLATTEN_EXIT_SLIP, null).filledQty > 0 && p.status !== "open") n++;
        } catch {
          // no bid right now / window closing: the retry below (or settlement) handles it
        }
        // not flat (partial / nothing to sell into) and still tradable: a triggered bracket retries every second
        if (p.status === "open" && p.qty > EPS && p.windowId === cur.id && liveWindow()) {
          const now = ctx.now();
          const kb: Bracket = {
            id: id("br"),
            positionId: p.id,
            windowId: p.windowId,
            tpCents: null,
            slCents: null,
            btcStop: null,
            timeExitSec: null,
            maxSlippageCents: FLATTEN_EXIT_SLIP,
            status: "triggered",
            triggerReason: reason,
            triggeredTs: now,
            exitOrderId: null,
            createdTs: now,
            updatedTs: now,
          };
          brackets.set(kb.id, kb);
          p.bracketId = kb.id;
          p.updatedTs = now;
          touchB(kb);
          touchP(p);
          notice("error", "Kill switch: not flat yet", `${sideName(p.side)} ${p.qty.toFixed(1)} still open; retrying every second until flat.`);
        }
      }
      flush();
      return n;
    },
    cancelRestingBuys(mode) {
      let n = 0;
      for (const o of working()) {
        if (o.action !== "buy" || o.mode !== mode) continue;
        o.status = "cancelled";
        o.error = "Trading disabled (daily loss limit)";
        o.updatedTs = ctx.now();
        pendingBracket.delete(o.id);
        touchO(o);
        n++;
      }
      flush();
      return n;
    },
    onSecond: (ts) => evaluate(ts),
    onRollover: (settled) => settleWindow(settled),
    snapshot: () => ({
      orders: newestFirst([...orders.values()], (o) => o.createdTs).slice(0, LIST_CAP).map((o) => ({ ...o })),
      fills: fills.slice(0, LIST_CAP).map((f) => ({ ...f })),
      positions: [...positions.values()].map((p) => ({ ...p })),
      brackets: [...brackets.values()].map((b) => ({ ...b })),
    }),
  };
  return self;
}

/** @deprecated name kept for older imports */
export const createMockPaperTrading = createMockTrading;
