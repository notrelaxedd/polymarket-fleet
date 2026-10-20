// Trading core: order routing (paper / live executor), fills -> positions + P&L, OCO bracket watcher,
// window settlement, live reconcile. OWNER: trading-backend.
//
// Accounting (USD unless noted; contract prices in cents):
//   buy fill   qty += q; costUsd += q * p / 100; avgCents = costUsd * 100 / qty; fees are realized at once.
//   sell fill  realized += q * (p - avg) / 100 - fee; qty -= q; costUsd = qty * avg / 100 (avg unchanged).
//   settlement every open contract pays 100 (winning side) or 0: realized += qty * (payout - avg) / 100.
//   Position.realizedPnlUsd is NET of every fee paid; feesUsd is the informational total. An unknown fee
//   ([FEE]: no confirmed schedule and no settings override) is stored as Fill.feeUsd = null and counts 0.
//   Mark = this side's best bid (fresh odds, else a fresh book); unrealized = qty * (mark - avg) / 100.
//
// Concurrency: everything that sizes an order from a position runs under a per-(mode, window, side) lock, so a
// manual close, a bracket exit and the kill switch can never sell the same contracts twice. Fills are applied
// synchronously inside one SQLite transaction each.

import { randomUUID } from "node:crypto";
import type {
  BookLevels,
  Bracket,
  BracketInput,
  ClosePositionRequest,
  Fill,
  MarketWindow,
  Notice,
  Order,
  OrderReason,
  OrderStatus,
  PlaceOrderRequest,
  PlaceOrderResponse,
  Position,
  Side,
  TradingMode,
} from "../../../shared/protocol.ts";
import { STALE_AFTER_MS } from "../../../shared/protocol.ts";
import { bracketEmpty, evaluateBracket, sideBook } from "../../../shared/paper.ts";
import { etDay } from "../../../shared/risk.ts";
import type {
  ExecContext,
  ExecFill,
  ExecResult,
  Executor,
  ExternalOrderUpdate,
  ModuleDeps,
  OrderQuery,
  RemotePosition,
} from "../modules/types.ts";
import { ApiError, badRequest, notFound } from "../server/errors.ts";
import { marketableLimit, qtyForUsd, round, walkBook } from "./fill.ts";
import { PaperExecutor, bookTradeable, type SnapshotUse } from "./paper.ts";
import { TradingStore, WORKING_STATUSES, isUniqueViolation } from "./store.ts";
import { parseBracket, parsePlaceOrder } from "./validate.ts";

/** Exit slippage when neither the bracket nor the entry order says otherwise, cents. */
export const DEFAULT_EXIT_SLIPPAGE_CENTS = 3;
/** A book older than this is not used to fill paper orders. */
export const BOOK_MAX_AGE_MS = 10_000;
/** BTC ticks older than this do not drive BTC stops. */
export const BTC_MAX_AGE_MS = 5_000;
/** A triggered bracket whose exit did not flatten the position retries this often. */
export const EXIT_RETRY_MS = 1_000;
/**
 * Exits that reached the executor and filled nothing back off (2s, 4s, 8s, ... up to this; one step less when the
 * side's top bid changed since), so a stuck exit does not write an order row and a broadcast every second.
 */
export const EXIT_RETRY_MAX_MS = 10_000;
/** Kill / flatten exits that did not get flat retry (via a triggered bracket) with at least this slippage cap. */
export const FLATTEN_EXIT_SLIPPAGE_CENTS = 10;
/** A "pending" order with no exchange id and no submit in flight this long is orphaned (crash mid-submit). */
export const PENDING_TIMEOUT_MS = 30_000;
/** Open positions of ended windows look for their window's result this often (missed "settled" events). */
export const SETTLE_RECHECK_MS = 10_000;
/**
 * A window with no result this long after its end (void / non-binary settlement, or the tracker gave up: it
 * polls for 6h) has its open positions closed at cost, so they stop holding risk exposure.
 */
export const UNSETTLED_GIVE_UP_MS = 6 * 3600_000;
/** Open-position marks are re-broadcast at most this often. */
export const MARK_BROADCAST_MS = 1_000;
/** Live orders still "working" this long after their window ended are closed locally. */
export const LIVE_EXPIRY_GRACE_MS = 60_000;
/** Snapshot / "recent" lists cover this much history. */
export const RECENT_MS = 24 * 3600_000;
/** Private-WS updates for an order whose submit has not returned yet are held this long. */
export const EXTERNAL_HOLD_MS = 30_000;

const EPS = 1e-6;
const TERMINAL: readonly OrderStatus[] = ["filled", "cancelled", "rejected"];
const sideName = (s: Side) => (s === "up" ? "Up" : "Down");
const usd = (v: number) => `${v < 0 ? "-" : "+"}$${Math.abs(v).toFixed(2)}`;
const fmtQty = (q: number) => (Math.round(q * 10) / 10).toFixed(1);
const REASON_LABEL: Record<OrderReason, string> = {
  manual: "Order",
  close: "Close",
  tp: "Take profit",
  sl: "Stop loss",
  btc_stop: "BTC stop",
  time_exit: "Time exit",
  kill: "Kill switch",
};

/** Serializes async work per key (FIFO). */
export class KeyedLock {
  private tails = new Map<string, Promise<void>>();

  async run<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const prev = this.tails.get(key) ?? Promise.resolve();
    let release!: () => void;
    const mine = new Promise<void>((r) => (release = r));
    const tail = prev.then(() => mine);
    this.tails.set(key, tail);
    await prev;
    try {
      return await fn();
    } finally {
      release();
      if (this.tails.get(key) === tail) this.tails.delete(key);
    }
  }

  /** something holds or waits for this key */
  busy(key: string): boolean {
    return this.tails.has(key);
  }
}

/** What one operation changed; flushed as one broadcast per message type. */
class Changes {
  orders = new Map<string, Order>();
  fills: Fill[] = [];
  positions = new Map<string, Position>();
  brackets = new Map<string, Bracket>();
  notices: Notice[] = [];
  order(o: Order) {
    this.orders.set(o.id, { ...o });
  }
  position(p: Position) {
    this.positions.set(p.id, { ...p });
  }
  bracket(b: Bracket) {
    this.brackets.set(b.id, { ...b });
  }
  get empty() {
    return !this.orders.size && !this.fills.length && !this.positions.size && !this.brackets.size && !this.notices.length;
  }
}

const posKey = (mode: TradingMode, windowId: string, side: Side) => `${mode}|${windowId}|${side}`;

export class TradingCore {
  readonly store: TradingStore;
  readonly paper: PaperExecutor;
  private readonly lock = new KeyedLock();
  private readonly exits = new Map<string, Promise<void>>(); // bracket id -> running exit
  private readonly exitLastAttempt = new Map<string, number>();
  private readonly exitFailNotified = new Set<string>();
  private readonly windowCache = new Map<string, MarketWindow>();
  private readonly heldExternal = new Map<string, { ts: number; updates: ExternalOrderUpdate[] }>();
  /** bracket id -> consecutive exit attempts that reached the executor and filled nothing, at which top bid */
  private readonly exitNoFill = new Map<string, { n: number; bidSig: string }>();
  /** orders whose executor submit (and result) is still in flight */
  private readonly inFlight = new Set<string>();
  /** window id -> last time settleEnded() looked it up */
  private readonly settleChecked = new Map<string, number>();
  /** position id -> last known bid (marks positions of ended windows until they settle) */
  private readonly lastBid = new Map<string, number>();
  private readonly unpricedNotified = new Set<string>();
  private lastMode: TradingMode | null = null;
  private liveBracketCache: Bracket[] | null = null;
  private lastMarks = new Map<string, number | null>();
  private lastMarkBroadcast = 0;
  private marksDirty = false;

  constructor(private readonly deps: ModuleDeps) {
    this.store = new TradingStore(deps.db.raw);
    this.paper = new PaperExecutor(() => deps.now());
    if (!this.store.clientIdUnique) {
      deps.logger.warn("trading: duplicate clientOrderIds in the orders table; idempotency relies on the in-lock check only");
    }
  }

  private now(): number {
    return this.deps.now();
  }

  // =================================================================== entry orders

  async placeOrder(reqIn: PlaceOrderRequest, opts: { reason?: OrderReason } = {}): Promise<PlaceOrderResponse> {
    const req = parsePlaceOrder(reqIn); // also guards in-process callers
    const reason = opts.reason ?? "manual";
    if (req.clientOrderId) {
      const dup = this.store.orderByClientId(req.clientOrderId);
      if (dup) return this.response(dup);
    }
    const settings = this.deps.engine.getSettings();
    const mode = settings.mode;
    const win = this.deps.engine.currentWindow();
    const now0 = this.now();
    if (!win || now0 < win.startTs || now0 >= win.endTs) throw new ApiError(409, "no_market", "no live market window right now");
    const executor = this.executorFor(mode);
    if (mode === "live" && !this.deps.services.live.isSessionConfirmed(req.liveSession)) {
      throw new ApiError(409, "live_confirm_required", "confirm live trading for this session first");
    }
    const bracketIn = bracketEmpty(req.bracket)
      ? null
      : { ...req.bracket!, maxSlippageCents: req.bracket!.maxSlippageCents ?? req.maxSlippageCents };

    return this.lock.run(posKey(mode, win.id, req.side), async () => {
      // again under the lock: a double submit queued behind the first one must not buy twice
      if (req.clientOrderId) {
        const dup = this.store.orderByClientId(req.clientOrderId);
        if (dup) return this.response(dup);
      }
      const book = await this.freshBook(win);
      const now = this.now();
      if (now >= win.endTs) throw new ApiError(409, "no_market", "the window just closed");
      const q = this.sideQuote(win.id, req.side, book);
      const sb = book ? sideBook(book, req.side) : null;
      const refAsk = sb?.asks[0]?.price ?? q.ask;

      let limitCents: number;
      let qty: number;
      let addCostUsd: number;
      if (req.type === "marketable") {
        if (refAsk == null) throw new ApiError(409, "no_market", `no ${sideName(req.side)} ask to buy from`);
        limitCents = marketableLimit("buy", refAsk, req.maxSlippageCents);
        // contracts the budget buys walking the whole book (+ the rest at the cap): the IOC's requested size
        const all = sb ? walkBook({ action: "buy", levels: sb.asks, limitCents: Infinity, budgetUsd: req.usd }) : null;
        qty = round((all?.qty ?? 0) + qtyForUsd(all ? all.leftUsd : req.usd, limitCents), 6);
        addCostUsd = req.usd;
      } else {
        limitCents = marketableLimit("buy", req.limitCents!, 0); // grid + clamp
        qty = qtyForUsd(req.usd, limitCents);
        addCostUsd = round((qty * limitCents) / 100, 6);
      }
      if (!(qty > 0)) throw badRequest(`$${req.usd} buys less than 0.1 contract at ${limitCents}¢`);

      const existing = this.store.openPositionFor(mode, win.id, req.side);
      const order: Order = {
        id: randomUUID(),
        clientOrderId: req.clientOrderId ?? null,
        externalId: null,
        mode,
        windowId: win.id,
        instrument: req.side === "up" ? win.upInstrument : win.downInstrument,
        side: req.side,
        action: "buy",
        type: req.type,
        limitCents,
        maxSlippageCents: req.maxSlippageCents,
        qty,
        notionalUsd: req.usd,
        filledQty: 0,
        avgFillCents: null,
        feesUsd: 0,
        status: "pending",
        reason,
        bracketId: null,
        positionId: existing?.id ?? null,
        error: null,
        createdTs: now,
        updatedTs: now,
      };

      // pre-trade risk (alerts-risk owns the policy); a rejection is recorded as a rejected order
      const check = this.deps.services.risk.check({
        addCostUsd,
        windowCostUsd: this.windowCostAtRisk(win.id, mode),
        positionCostUsd: existing?.costUsd ?? 0,
        reducing: false,
      });
      if (!check.ok) {
        const ch = new Changes();
        order.status = "rejected";
        order.error = check.reason;
        const dup = this.insertOnce(order, () => this.store.insertOrder(order));
        if (dup) return this.response(dup);
        ch.order(order);
        this.flush(ch);
        throw new ApiError(check.code === "trading_disabled" ? 423 : 403, check.code, check.reason);
      }

      const ch = new Changes();
      const dup = this.insertOnce(order, () =>
        this.store.tx(() => {
          this.store.insertOrder(order);
          this.store.setOrderMeta(order.id, { bracket: bracketIn, refCents: refAsk ?? null });
        }),
      );
      if (dup) return this.response(dup); // same key raced in on the other side's lock
      ch.order(order);
      const ctx: ExecContext = { window: win, book, odds: this.deps.engine.latestOdds(now), feeCentsPerContract: settings.feeCentsPerContract };
      await this.execute(executor, order, ctx, refAsk ?? null, ch);
      this.flush(ch);
      this.logOrder(order);
      this.raiseForResult(order, mode);
      return this.response(order);
    });
  }

  // =================================================================== exits

  async closePosition(id: string, req: ClosePositionRequest & { reason?: OrderReason } = {}): Promise<PlaceOrderResponse> {
    return this.exit(id, { reason: req.reason ?? "close", maxSlippageCents: req.maxSlippageCents ?? null, bracketId: null });
  }

  /** Marketable-limit sell of everything not already working in an exit; capped by max slippage (IOC). */
  private async exit(
    positionId: string,
    opts: { reason: OrderReason; maxSlippageCents: number | null; bracketId: string | null },
  ): Promise<PlaceOrderResponse> {
    const p0 = this.store.position(positionId);
    if (!p0) throw notFound("position not found");
    return this.lock.run(posKey(p0.mode, p0.windowId, p0.side), async () => {
      const pos = this.store.position(positionId)!;
      if (pos.status !== "open") throw badRequest(`position is ${pos.status}`);
      const win = this.deps.engine.currentWindow();
      if (!win || win.id !== pos.windowId || this.now() >= win.endTs) {
        throw new ApiError(409, "no_market", "this window has closed; the position settles at expiry");
      }
      const executor = this.executorFor(pos.mode, "exit");
      const qty = round(pos.qty - this.workingSellQty(pos.id), 6);
      if (qty <= EPS) throw new ApiError(409, "no_market", "an exit order for this position is already working");
      const book = await this.freshBook(win);
      const now = this.now();
      if (book && !bookTradeable(book)) throw new ApiError(409, "no_market", `market not open (${book.state})`);
      const sb = book ? sideBook(book, pos.side) : null;
      // paper can only sell into a real book; live may fall back to the top of book
      const refBid = sb?.bids[0]?.price ?? (pos.mode === "live" ? this.sideQuote(win.id, pos.side, null).bid : null);
      if (refBid == null) throw new ApiError(409, "no_market", `no ${sideName(pos.side)} bid to sell into`);
      const bracket = pos.bracketId ? this.store.bracket(pos.bracketId) : null;
      const slip =
        opts.maxSlippageCents ??
        bracket?.maxSlippageCents ??
        this.store.lastEntryOrder(pos.id)?.maxSlippageCents ??
        DEFAULT_EXIT_SLIPPAGE_CENTS;
      const order: Order = {
        id: randomUUID(),
        clientOrderId: null,
        externalId: null,
        mode: pos.mode,
        windowId: pos.windowId,
        instrument: pos.instrument,
        side: pos.side,
        action: "sell",
        type: "marketable",
        limitCents: marketableLimit("sell", refBid, slip),
        maxSlippageCents: slip,
        qty,
        notionalUsd: null,
        filledQty: 0,
        avgFillCents: null,
        feesUsd: 0,
        status: "pending",
        reason: opts.reason,
        bracketId: opts.bracketId,
        positionId: pos.id,
        error: null,
        createdTs: now,
        updatedTs: now,
      };
      const ch = new Changes();
      this.store.tx(() => {
        this.store.insertOrder(order);
        this.store.setOrderMeta(order.id, { bracket: null, refCents: refBid });
        if (opts.bracketId) {
          const b = this.store.bracket(opts.bracketId);
          if (b) {
            b.exitOrderId = order.id;
            b.updatedTs = now;
            this.saveBracket(b, ch);
          }
        }
      });
      ch.order(order);
      const ctx: ExecContext = {
        window: win,
        book,
        odds: this.deps.engine.latestOdds(now),
        feeCentsPerContract: this.deps.engine.getSettings().feeCentsPerContract,
      };
      await this.execute(executor, order, ctx, refBid, ch);
      this.flush(ch);
      this.logOrder(order);
      this.raiseForResult(order, pos.mode);
      return this.response(order);
    });
  }

  async cancelOrder(id: string): Promise<Order> {
    const o = this.store.order(id);
    if (!o) throw notFound("order not found");
    if (!WORKING_STATUSES.includes(o.status)) throw badRequest(`order is already ${o.status}`);
    const executor = this.executorFor(o.mode, "exit");
    try {
      await executor.cancel(o);
    } catch (err) {
      throw new ApiError(502, "exchange_error", `cancel failed: ${(err as Error).message}`);
    }
    const ch = new Changes();
    const cur = this.store.order(id)!; // fills may have landed while the cancel was in flight
    if (WORKING_STATUSES.includes(cur.status)) {
      cur.status = "cancelled";
      cur.updatedTs = this.now();
      this.store.updateOrder(cur);
      ch.order(cur);
    }
    this.flush(ch);
    return cur;
  }

  async cancelAll(reason: OrderReason): Promise<number> {
    let n = 0;
    for (const o of this.store.workingOrders()) {
      try {
        await this.cancelOrder(o.id);
        n++;
      } catch (err) {
        this.deps.logger.warn({ err: (err as Error).message, orderId: o.id, reason }, "cancel failed");
      }
    }
    return n;
  }

  /**
   * One marketable exit per open position of the current window. Whatever is still open afterwards (thin book,
   * slippage cap, no bid, live executor unavailable) gets a triggered bracket with reason `reason` and at least
   * FLATTEN_EXIT_SLIPPAGE_CENTS of slippage, so the watcher keeps retrying every second until flat or the window
   * ends; those positions are reported in one error notice.
   */
  async flattenAll(reason: OrderReason): Promise<number> {
    const cur = this.deps.engine.currentWindow();
    let n = 0;
    const left: string[] = [];
    for (const p of this.store.openPositions()) {
      if (!cur || p.windowId !== cur.id) continue; // ended windows cannot trade; they settle
      let why: string | null = null;
      try {
        const r = await this.exit(p.id, { reason, maxSlippageCents: null, bracketId: null });
        if (r.order.filledQty > 0) n++;
      } catch (err) {
        why = (err as Error).message;
        this.deps.logger.warn({ err: why, positionId: p.id, mode: p.mode, reason }, "flatten failed");
      }
      const after = this.store.position(p.id);
      if (after?.status !== "open") continue;
      this.armForcedExit(after, reason);
      left.push(`${sideName(after.side)} ${fmtQty(after.qty)}${after.mode === "live" ? " (LIVE)" : ""}${why ? `: ${why}` : ""}`);
    }
    if (left.length) {
      const ch = new Changes();
      ch.notices.push(
        this.notice("error", `${REASON_LABEL[reason]}: not flat yet`, `Still open: ${left.join("; ")}. Retrying every second until flat or the window ends.`),
      );
      this.flush(ch);
    }
    return n;
  }

  /** Keep exiting an open position through the bracket retry loop (kill / flatten that did not get flat). */
  private armForcedExit(pos: Position, reason: OrderReason): void {
    const now = this.now();
    const ch = new Changes();
    let id: string | null = null;
    this.store.tx(() => {
      const p = this.store.position(pos.id);
      if (!p || p.status !== "open") return;
      const base = p.bracketId ? this.store.bracket(p.bracketId) : null;
      const slip = Math.max(
        FLATTEN_EXIT_SLIPPAGE_CENTS,
        base?.maxSlippageCents ?? this.store.lastEntryOrder(p.id)?.maxSlippageCents ?? DEFAULT_EXIT_SLIPPAGE_CENTS,
      );
      if (base && (base.status === "active" || base.status === "triggered")) {
        const b: Bracket = { ...base, status: "triggered", triggerReason: reason, triggeredTs: base.triggeredTs ?? now, maxSlippageCents: slip, updatedTs: now };
        this.saveBracket(b, ch);
        id = b.id;
        return;
      }
      const b: Bracket = {
        id: randomUUID(),
        positionId: p.id,
        windowId: p.windowId,
        tpCents: null,
        slCents: null,
        btcStop: null,
        timeExitSec: null,
        maxSlippageCents: slip,
        status: "triggered",
        triggerReason: reason,
        triggeredTs: now,
        exitOrderId: null,
        createdTs: now,
        updatedTs: now,
      };
      this.saveBracket(b, ch, true);
      p.bracketId = b.id;
      p.updatedTs = now;
      this.store.updatePosition(p);
      ch.position(p);
      id = b.id;
    });
    if (id) {
      this.exitLastAttempt.set(id, now); // flattenAll just tried: the next attempt is a retry
      this.exitFailNotified.add(id); // flattenAll reports it
    }
    this.flush(ch);
  }

  // =================================================================== brackets

  setBracket(positionId: string, inputIn: BracketInput | null): Bracket | null {
    const input = parseBracket(inputIn);
    const pos = this.store.position(positionId);
    if (!pos) throw notFound("position not found");
    if (pos.status !== "open") throw badRequest(`position is ${pos.status}`);
    // a leg already through the market would fire on the next watcher pass: refuse instead of exiting by accident
    if (input) {
      const bid = this.sideQuote(pos.windowId, pos.side, this.deps.engine.currentBook()).bid;
      const side = pos.side === "up" ? "Up" : "Down";
      if (bid != null && input.tpCents != null && input.tpCents <= bid)
        throw badRequest(`tpCents: ${input.tpCents}¢ is at or below the ${side} bid (${bid}¢) and would trigger immediately; use Close to exit now`);
      if (bid != null && input.slCents != null && input.slCents >= bid)
        throw badRequest(`slCents: ${input.slCents}¢ is at or above the ${side} bid (${bid}¢) and would trigger immediately; use Close to exit now`);
    }
    const ch = new Changes();
    const b = this.store.tx(() => this.applyBracket(pos, input, ch));
    this.flush(ch);
    setImmediate(() => this.safeEvaluate());
    return b;
  }

  /** Create / update / remove the OCO bracket of an open position (inside a transaction). */
  private applyBracket(pos: Position, input: BracketInput | null, ch: Changes): Bracket | null {
    const now = this.now();
    const existing = pos.bracketId ? this.store.bracket(pos.bracketId) : null;
    if (existing?.status === "triggered") throw badRequest("the bracket already triggered; its exit is working");
    const live = existing && existing.status === "active" ? existing : null;
    if (bracketEmpty(input)) {
      if (live) {
        live.status = "cancelled";
        live.updatedTs = now;
        this.saveBracket(live, ch);
      }
      if (pos.bracketId) {
        pos.bracketId = null;
        pos.updatedTs = now;
        this.store.updatePosition(pos);
        ch.position(pos);
      }
      return null;
    }
    const b = input!;
    const slip =
      b.maxSlippageCents ??
      live?.maxSlippageCents ??
      this.store.lastEntryOrder(pos.id)?.maxSlippageCents ??
      DEFAULT_EXIT_SLIPPAGE_CENTS;
    const next: Bracket = {
      id: live?.id ?? randomUUID(),
      positionId: pos.id,
      windowId: pos.windowId,
      tpCents: b.tpCents ?? null,
      slCents: b.slCents ?? null,
      btcStop: b.btcStop ?? null,
      timeExitSec: b.timeExitSec ?? null,
      maxSlippageCents: slip,
      status: "active",
      triggerReason: null,
      triggeredTs: null,
      exitOrderId: null,
      createdTs: live?.createdTs ?? now,
      updatedTs: now,
    };
    this.saveBracket(next, ch, !live);
    if (pos.bracketId !== next.id) {
      pos.bracketId = next.id;
      pos.updatedTs = now;
      this.store.updatePosition(pos);
      ch.position(pos);
    }
    return next;
  }

  private saveBracket(b: Bracket, ch: Changes, insert = false): void {
    if (insert) this.store.insertBracket(b);
    else this.store.updateBracket(b);
    this.liveBracketCache = null;
    if (b.status === "done" || b.status === "cancelled") {
      this.exitNoFill.delete(b.id);
      this.exitLastAttempt.delete(b.id);
      this.exitFailNotified.delete(b.id);
    }
    ch.bracket(b);
  }

  private liveBrackets(): Bracket[] {
    return (this.liveBracketCache ??= this.store.liveBrackets());
  }

  /**
   * The watcher: evaluate every active bracket of the current window against the side's bid, the BRTI proxy and
   * the clock; the first leg that fires triggers an exit and cancels the others (OCO). Triggered brackets whose
   * exit did not flatten the position (slippage cap, thin book) retry every EXIT_RETRY_MS.
   */
  evaluate(): void {
    const brackets = this.liveBrackets();
    if (!brackets.length) return;
    const now = this.now();
    const cur = this.deps.engine.currentWindow();
    const ch = new Changes();
    for (const b0 of brackets) {
      if (this.exits.has(b0.id)) continue;
      const pos = this.store.position(b0.positionId);
      if (!pos || pos.status !== "open") {
        const b = this.store.bracket(b0.id);
        if (b && (b.status === "active" || b.status === "triggered")) {
          b.status = b.status === "triggered" ? "done" : "cancelled";
          b.updatedTs = now;
          this.saveBracket(b, ch);
        }
        continue;
      }
      if (!cur || b0.windowId !== cur.id || now < cur.startTs || now >= cur.endTs) continue;
      if (b0.status === "active") {
        const q = this.sideQuote(cur.id, pos.side, this.deps.engine.currentBook());
        const tick = this.deps.engine.latestTick();
        const btc = tick && now - tick.ts <= BTC_MAX_AGE_MS ? tick.price : null;
        const reason = evaluateBracket(b0, pos.side, { now, sideBid: q.bid, btc, windowEndTs: cur.endTs });
        if (!reason) continue;
        const b = { ...b0, status: "triggered" as const, triggerReason: reason, triggeredTs: now, updatedTs: now };
        this.saveBracket(b, ch);
        const what =
          reason === "tp"
            ? `bid ${q.bid}¢ ≥ TP ${b.tpCents}¢`
            : reason === "sl"
              ? `bid ${q.bid}¢ ≤ SL ${b.slCents}¢`
              : reason === "btc_stop"
                ? `BTC ${btc?.toFixed(2)} ${pos.side === "up" ? "<" : ">"} ${b.btcStop}`
                : `T−${Math.max(0, Math.round((cur.endTs - now) / 1000))}s`;
        ch.notices.push(
          this.notice(reason === "tp" ? "info" : "warn", `${REASON_LABEL[reason]} triggered`, `${sideName(pos.side)} ${fmtQty(pos.qty)}: ${what}; exiting`),
        );
        this.deps.logger.info({ bracketId: b.id, positionId: pos.id, reason, bid: q.bid, btc }, "bracket triggered");
        this.startExit(b.id, pos.id, reason);
      } else if (now - (this.exitLastAttempt.get(b0.id) ?? 0) >= this.exitRetryDelay(b0.id, pos.side)) {
        this.startExit(b0.id, pos.id, b0.triggerReason ?? "sl");
      }
    }
    this.flush(ch);
  }

  private safeEvaluate(): void {
    try {
      this.evaluate();
    } catch (err) {
      this.deps.logger.error({ err }, "bracket watcher failed");
    }
  }

  /** Bid "signature" of a side: the retry backoff resets when it changes. */
  private bidSig(side: Side): string {
    const book = this.deps.engine.currentBook();
    const top = book ? sideBook(book, side).bids[0] : undefined;
    return top ? `${top.price}x${top.size}` : "none";
  }

  private exitRetryDelay(bracketId: string, side: Side): number {
    const st = this.exitNoFill.get(bracketId);
    if (!st || st.n <= 0) return EXIT_RETRY_MS;
    // a new top bid may fill: retry one step sooner (still backing off when the exchange keeps refusing)
    const steps = st.bidSig === this.bidSig(side) ? st.n : st.n - 1;
    return Math.min(EXIT_RETRY_MS * 2 ** steps, EXIT_RETRY_MAX_MS);
  }

  private startExit(bracketId: string, positionId: string, reason: OrderReason): void {
    const run = (async () => {
      const side = this.store.position(positionId)?.side ?? "up";
      const sig = this.bidSig(side);
      const prevExitId = this.store.bracket(bracketId)?.exitOrderId ?? null;
      try {
        const b = this.store.bracket(bracketId);
        const r = await this.exit(positionId, { reason, maxSlippageCents: b?.maxSlippageCents ?? null, bracketId });
        if (r.order.filledQty > 0) this.exitNoFill.delete(bracketId);
        if (r.position && r.position.status !== "open") {
          this.exitFailNotified.delete(bracketId);
          const ch = new Changes();
          ch.notices.push(
            this.notice(
              "success",
              `${REASON_LABEL[reason]} filled`,
              `Sold ${sideName(r.order.side)} ${fmtQty(r.order.filledQty)} @ ${r.order.avgFillCents?.toFixed(1)}¢ · ${usd(r.position.realizedPnlUsd)}`,
            ),
          );
          this.flush(ch);
        }
      } catch (err) {
        const msg = (err as Error).message;
        // a racing close / kill / settlement already flattened it: the bracket is finished, nothing failed
        if (this.store.position(positionId)?.status !== "open") return;
        // an order row was written and filled nothing: back off while the book stays the same
        const exitId = this.store.bracket(bracketId)?.exitOrderId ?? null;
        if (exitId && exitId !== prevExitId && !((this.store.order(exitId)?.filledQty ?? 0) > 0)) {
          this.exitNoFill.set(bracketId, { n: (this.exitNoFill.get(bracketId)?.n ?? 0) + 1, bidSig: sig });
        }
        this.deps.logger.warn({ bracketId, positionId, reason, err: msg }, "bracket exit failed; retrying");
        if (!this.exitFailNotified.has(bracketId)) {
          this.exitFailNotified.add(bracketId);
          const ch = new Changes();
          ch.notices.push(this.notice("error", `${REASON_LABEL[reason]} exit failed`, `${msg}. Retrying every second.`));
          this.flush(ch);
        }
      } finally {
        this.exits.delete(bracketId);
        this.exitLastAttempt.set(bracketId, this.now());
      }
    })();
    this.exits.set(bracketId, run);
  }

  /** Resolves once every running bracket exit finished (tests, shutdown). */
  async idle(): Promise<void> {
    while (this.exits.size) await Promise.allSettled([...this.exits.values()]);
  }

  // =================================================================== resting paper orders, expiry, settlement

  /**
   * A new Up book: fill resting paper limit orders from crossing liquidity that is new in this snapshot (at the
   * limit; oldest order first, each contract of the book used once), then re-check brackets.
   */
  onBook(book: BookLevels): void {
    const fee = this.deps.engine.getSettings().feeCentsPerContract;
    const ch = new Changes();
    const working = this.store.workingOrders();
    this.paper.prune(new Set(working.map((o) => o.id)));
    const use: SnapshotUse = new Map();
    for (const o of working) {
      if (o.mode !== "paper" || o.type !== "limit" || o.status === "pending" || o.windowId !== book.windowId) continue;
      const f = this.paper.matchResting(o, book, fee, use);
      if (!f) continue;
      this.store.tx(() => {
        const meta = this.store.orderMeta(o.id);
        this.recordFill(o, f, meta.refCents, ch);
        o.status = o.filledQty >= o.qty - EPS ? "filled" : "partial";
        o.updatedTs = this.now();
        this.store.updateOrder(o);
        ch.order(o);
      });
      if (o.status === "filled") {
        ch.notices.push(
          this.notice("success", "Limit order filled", `Bought ${sideName(o.side)} ${fmtQty(o.filledQty)} @ ${o.limitCents}¢`),
        );
      }
      this.logOrder(o);
    }
    this.flush(ch);
    this.marksDirty = true;
    this.safeEvaluate();
  }

  /**
   * Cancel working orders whose window ended (paper at once; live after a grace period without an update), and
   * orphaned "pending" orders (no exchange id, no submit in flight for PENDING_TIMEOUT_MS: the worker died or
   * threw between writing the row and recording the executor's answer).
   */
  expireOrders(): void {
    const now = this.now();
    const ch = new Changes();
    for (const o of this.store.workingOrders()) {
      if (this.isOrphan(o) && now - o.updatedTs >= PENDING_TIMEOUT_MS) {
        this.resolveOrphan(o, "no answer from the executor", ch);
        continue;
      }
      const w = this.windowOf(o.windowId);
      const end = w?.endTs ?? null;
      if (end == null || now < end + (o.mode === "live" ? LIVE_EXPIRY_GRACE_MS : 0)) continue;
      o.status = "cancelled";
      o.error = o.error ?? "window closed";
      o.updatedTs = now;
      this.store.updateOrder(o);
      ch.order(o);
      if (o.filledQty <= 0 && o.action === "buy") {
        ch.notices.push(this.notice("info", "Order expired", `${sideName(o.side)} limit @ ${o.limitCents}¢ cancelled at window close`));
      }
    }
    this.flush(ch);
  }

  /** A window got its result: every open position in it pays 100¢ (winning side) or 0 per contract. */
  settle(w: MarketWindow): void {
    if (!w.result) return;
    this.windowCache.set(w.id, w);
    const now = this.now();
    const ch = new Changes();
    this.store.tx(() => {
      for (const o of this.store.workingOrders()) {
        if (o.windowId !== w.id) continue;
        o.status = "cancelled";
        o.error = o.error ?? "window settled";
        o.updatedTs = now;
        this.store.updateOrder(o);
        ch.order(o);
      }
      for (const p of this.store.openPositions()) {
        if (p.windowId !== w.id) continue;
        const payout = p.side === w.result ? 100 : 0;
        const pnl = round((p.qty * (payout - p.avgCents)) / 100, 6);
        this.store.addPnl({ ts: now, day: etDay(now), mode: p.mode, windowId: p.windowId, positionId: p.id, kind: "settle", amountUsd: pnl });
        const settledQty = p.qty;
        p.realizedPnlUsd = round(p.realizedPnlUsd + pnl, 6);
        p.qty = 0;
        p.costUsd = 0;
        p.status = "settled";
        p.updatedTs = now;
        p.closedTs = now;
        this.store.updatePosition(p);
        this.closeBracketOf(p, ch);
        ch.position(p);
        ch.notices.push(
          this.notice(
            payout ? "success" : "info",
            `Settled ${w.result === "up" ? "UP" : "DOWN"}`,
            `${sideName(p.side)} ${fmtQty(settledQty)} @ ${p.avgCents.toFixed(1)}¢ paid ${payout}¢ · ${usd(p.realizedPnlUsd)}`,
          ),
        );
        this.deps.logger.info({ positionId: p.id, windowId: w.id, result: w.result, pnl, realized: p.realizedPnlUsd }, "position settled");
      }
    });
    this.flush(ch);
  }

  /** Startup: resolve orders a restart interrupted, settle windows that settled while down, expire stale orders. */
  recover(): void {
    const ch = new Changes();
    for (const o of this.store.workingOrders()) if (this.isOrphan(o)) this.resolveOrphan(o, "interrupted by a restart", ch);
    this.flush(ch);
    for (const p of this.store.openPositions()) {
      const w = this.windowOf(p.windowId, true);
      if (w?.result) this.settle(w);
    }
    this.expireOrders();
  }

  /** A "pending" row whose submit is not running and that never got an exchange id. */
  private isOrphan(o: Order): boolean {
    return o.status === "pending" && !o.externalId && !this.inFlight.has(o.id);
  }

  /**
   * Close an orphaned pending order locally. Paper: nothing was simulated. Live: the exchange never confirmed it
   * (the executor has no order lookup); an IOC that did execute shows up in the next reconcile.
   */
  private resolveOrphan(o: Order, why: string, ch: Changes): void {
    o.status = "cancelled";
    o.error = o.mode === "live" ? `${why}; the exchange never confirmed it (reconcile adopts any fill)` : why;
    o.updatedTs = this.now();
    this.store.updateOrder(o);
    ch.order(o);
    ch.notices.push(
      this.notice("warn", "Order interrupted", `${sideName(o.side)} ${o.action} ${o.mode === "live" ? "(LIVE) " : ""}${why}; marked cancelled`),
    );
    this.deps.logger.warn({ orderId: o.id, mode: o.mode, action: o.action, why }, "orphaned pending order cancelled");
  }

  /** Contracts of a position still working in exits (orphaned pending rows excluded: they hold nothing). */
  private workingSellQty(positionId: string): number {
    let q = 0;
    for (const o of this.store.workingSells(positionId)) if (!this.isOrphan(o)) q += o.qty - o.filledQty;
    return q;
  }

  /**
   * Watcher: open positions of windows that ended. A result the "settled" event missed settles them; with no
   * result UNSETTLED_GIVE_UP_MS after the end (void / non-binary settlement: the tracker records no result) they
   * are closed at cost (ledger kind "void", $0) so they stop counting as exposure.
   */
  settleEnded(): void {
    const now = this.now();
    const cur = this.deps.engine.currentWindow();
    const looked = new Set<string>();
    for (const p of this.store.openPositions()) {
      if ((cur && p.windowId === cur.id) || looked.has(p.windowId)) continue;
      looked.add(p.windowId);
      if (now - (this.settleChecked.get(p.windowId) ?? -Infinity) < SETTLE_RECHECK_MS) continue;
      this.settleChecked.set(p.windowId, now);
      const w = this.windowOf(p.windowId, true);
      if (w?.result) {
        this.settle(w);
        continue;
      }
      const end = w?.endTs ?? p.openedTs;
      if (now >= end + UNSETTLED_GIVE_UP_MS) this.closeUnsettled(p.windowId);
    }
    for (const id of this.settleChecked.keys()) if (!looked.has(id)) this.settleChecked.delete(id);
  }

  private closeUnsettled(windowId: string): void {
    const now = this.now();
    const ch = new Changes();
    const hours = Math.round(UNSETTLED_GIVE_UP_MS / 3600_000);
    this.store.tx(() => {
      for (const o of this.store.workingOrders()) {
        if (o.windowId !== windowId) continue;
        o.status = "cancelled";
        o.error = o.error ?? "window never settled";
        o.updatedTs = now;
        this.store.updateOrder(o);
        ch.order(o);
      }
      for (const p of this.store.openPositions()) {
        if (p.windowId !== windowId) continue;
        this.store.addPnl({ ts: now, day: etDay(now), mode: p.mode, windowId, positionId: p.id, kind: "void", amountUsd: 0 });
        const qty = p.qty;
        p.qty = 0;
        p.costUsd = 0;
        p.status = "closed";
        p.updatedTs = now;
        p.closedTs = now;
        this.store.updatePosition(p);
        this.closeBracketOf(p, ch);
        ch.position(p);
        ch.notices.push(
          this.notice(
            "warn",
            "Window never settled",
            `${sideName(p.side)} ${fmtQty(qty)} @ ${p.avgCents.toFixed(1)}¢${p.mode === "live" ? " (LIVE)" : ""}: no result ${hours}h after the close (void?). Closed at cost, P&L $0.00; check the exchange.`,
          ),
        );
        this.deps.logger.warn({ positionId: p.id, windowId, qty }, "window never settled: position closed at cost");
      }
    });
    this.flush(ch);
  }

  // =================================================================== marks

  /** Odds moved: re-check brackets; re-broadcast open-position marks at most once per MARK_BROADCAST_MS. */
  onQuote(): void {
    this.marksDirty = true;
    this.safeEvaluate();
    this.maybeBroadcastMarks();
  }

  maybeBroadcastMarks(force = false): void {
    const now = this.now();
    if (!force && (!this.marksDirty || now - this.lastMarkBroadcast < MARK_BROADCAST_MS)) return;
    this.marksDirty = false;
    this.lastMarkBroadcast = now;
    const changed: Position[] = [];
    const open = new Set<string>();
    for (const p of this.store.openPositions()) {
      const m = this.withMark(p);
      open.add(p.id);
      if (this.lastMarks.get(p.id) !== m.markCents) {
        this.lastMarks.set(p.id, m.markCents);
        changed.push(m);
      }
    }
    for (const id of this.lastMarks.keys()) if (!open.has(id)) this.lastMarks.delete(id);
    for (const id of this.lastBid.keys()) if (!open.has(id)) this.lastBid.delete(id);
    if (changed.length) {
      this.deps.broadcast({ type: "positions", data: changed });
      this.refreshRisk();
    }
  }

  /** Best bid / ask of a side in the current window: fresh odds first, else a fresh book. */
  private sideQuote(windowId: string, side: Side, book: BookLevels | null): { bid: number | null; ask: number | null } {
    const cur = this.deps.engine.currentWindow();
    if (!cur || cur.id !== windowId) return { bid: null, ask: null };
    const now = this.now();
    let bid: number | null = null;
    let ask: number | null = null;
    const o = this.deps.engine.latestOdds(now);
    if (o && o.windowId === windowId && now - o.ts <= STALE_AFTER_MS) {
      bid = side === "up" ? o.upBid : o.downBid;
      ask = side === "up" ? o.upAsk : o.downAsk;
    }
    if ((bid == null || ask == null) && book && book.windowId === windowId && now - book.ts <= BOOK_MAX_AGE_MS) {
      const sb = sideBook(book, side);
      bid ??= sb.bids[0]?.price ?? null;
      ask ??= sb.asks[0]?.price ?? null;
    }
    return { bid, ask };
  }

  withMark(p: Position): Position {
    if (p.status === "open") {
      let mark = this.sideQuote(p.windowId, p.side, this.deps.engine.currentBook()).bid;
      if (mark != null) this.lastBid.set(p.id, mark);
      // ended window awaiting its settlement: the last bid seen (no quote exists any more)
      else if (this.deps.engine.currentWindow()?.id !== p.windowId) mark = this.lastBid.get(p.id) ?? null;
      return { ...p, markCents: mark, unrealizedPnlUsd: mark == null ? null : round((p.qty * (mark - p.avgCents)) / 100, 4) };
    }
    if (p.status === "settled") {
      const w = this.windowOf(p.windowId);
      const mark = w?.result ? (w.result === p.side ? 100 : 0) : null;
      return { ...p, markCents: mark, unrealizedPnlUsd: 0 };
    }
    return { ...p, markCents: null, unrealizedPnlUsd: 0 };
  }

  // =================================================================== queries

  orders(q: OrderQuery = {}): Order[] {
    return this.store.orders({ limit: clampLimit(q.limit), windowId: q.windowId, status: q.status });
  }

  fills(q: { limit?: number; windowId?: string } = {}): Fill[] {
    return this.store.fills({ limit: clampLimit(q.limit), windowId: q.windowId });
  }

  positions(q: { status?: "open" | "all"; limit?: number } = {}): Position[] {
    const limit = clampLimit(q.limit);
    const rows = q.status === "open" ? this.store.openPositions().slice(0, limit) : this.store.recentPositions(0, limit);
    return rows.map((p) => this.withMark(p));
  }

  brackets(q: { status?: "active" | "all" } = {}): Bracket[] {
    if (q.status === "active") return this.store.liveBrackets().reverse();
    return this.store.recentBrackets(this.now() - RECENT_MS);
  }

  snapshot() {
    const since = this.now() - RECENT_MS;
    return {
      orders: this.store.orders({ limit: 200, sinceTs: since }),
      fills: this.store.fills({ limit: 200, sinceTs: since }),
      positions: this.store.recentPositions(since, 200).map((p) => this.withMark(p)),
      brackets: this.store.recentBrackets(since),
    };
  }

  riskInputs(windowId: string | null) {
    const mode = this.deps.engine.getSettings().mode;
    const now = this.now();
    let unrealized = 0;
    let exposure = 0;
    for (const p of this.store.openPositions()) {
      if (p.mode !== mode) continue;
      unrealized += this.withMark(p).unrealizedPnlUsd ?? 0;
      exposure += p.costUsd;
    }
    for (const o of this.store.workingOrders()) if (o.mode === mode && o.action === "buy") exposure += restingCost(o);
    return {
      dailyRealizedPnlUsd: round(this.store.realizedForDay(etDay(now), mode), 2),
      unrealizedPnlUsd: round(unrealized, 2),
      openExposureUsd: round(exposure, 2),
      windowLossUsedUsd: windowId ? round(this.windowCostAtRisk(windowId, mode), 2) : 0,
    };
  }

  /** Cost at risk in one window: open cost + resting buys + realized loss so far (fees included). */
  private windowCostAtRisk(windowId: string, mode: TradingMode): number {
    let cost = 0;
    for (const p of this.store.openPositions()) if (p.mode === mode && p.windowId === windowId) cost += p.costUsd;
    for (const o of this.store.workingOrders()) {
      if (o.mode === mode && o.windowId === windowId && o.action === "buy") cost += restingCost(o);
    }
    return cost + Math.max(0, -this.store.realizedForWindow(windowId, mode));
  }

  // =================================================================== live: async updates + reconcile

  applyExternal(u: ExternalOrderUpdate): void {
    const o = this.store.orderByExternalId(u.externalId);
    if (!o) {
      // the private WS can beat the REST submit's response: hold the update until the order knows its id
      this.pruneHeld();
      const h = this.heldExternal.get(u.externalId) ?? { ts: this.now(), updates: [] };
      h.updates.push(u);
      this.heldExternal.set(u.externalId, h);
      return;
    }
    const ch = new Changes();
    this.store.tx(() => this.applyUpdate(o, u, ch));
    this.flush(ch);
  }

  private applyUpdate(o: Order, u: ExternalOrderUpdate, ch: Changes): void {
    const wasTerminal = TERMINAL.includes(o.status);
    if (u.fill) this.recordFill(o, u.fill, this.store.orderMeta(o.id).refCents, ch);
    if (u.status && !(wasTerminal && !TERMINAL.includes(u.status))) o.status = this.finalStatus(o, u.status);
    else if (u.fill && !wasTerminal) o.status = o.filledQty >= o.qty - EPS ? "filled" : "partial";
    if (u.error !== undefined && u.error !== null) o.error = u.error;
    o.updatedTs = this.now();
    this.store.updateOrder(o);
    ch.order(o);
  }

  private pruneHeld(): void {
    const cutoff = this.now() - EXTERNAL_HOLD_MS;
    for (const [k, h] of this.heldExternal) if (h.ts < cutoff) this.heldExternal.delete(k);
  }

  /**
   * Align local live positions of the current window with the exchange. Contracts that vanished (sold on the
   * exchange site, a missed exit fill) book their estimated result to the ledger (kind "reconcile", at the
   * side's bid; $0 when there is no bid), so daily / window loss limits see it. Positions only the exchange
   * knows are adopted at the exchange's average, else at the mark (fee flagged unknown); never at 0¢. A side
   * with a submit in flight or a working order is skipped: its fills are still arriving.
   */
  reconcile(remote: RemotePosition[]): void {
    const now = this.now();
    const ch = new Changes();
    const byInst = new Map(remote.map((r) => [r.instrument, r]));
    const cur = this.deps.engine.currentWindow();
    const book = this.deps.engine.currentBook();
    const workingKeys = new Set(this.store.workingOrders().map((o) => posKey(o.mode, o.windowId, o.side)));
    const busy = (windowId: string, side: Side) => {
      const k = posKey("live", windowId, side);
      return workingKeys.has(k) || this.lock.busy(k);
    };
    this.store.tx(() => {
      const seen = new Set<string>();
      for (const p of this.store.openPositions()) {
        if (p.mode !== "live") continue;
        seen.add(p.instrument);
        const r = byInst.get(p.instrument);
        const rq = r ? round(r.qty, 6) : 0;
        if (Math.abs(rq - p.qty) <= EPS) continue;
        // ended windows: the exchange drops settled positions before we see the settlement; leave them to settle()
        if (!cur || p.windowId !== cur.id) continue;
        if (busy(p.windowId, p.side)) {
          this.deps.logger.debug({ positionId: p.id, local: p.qty, remote: rq }, "live reconcile: order working; skipped");
          continue;
        }
        const oldQty = p.qty;
        const q = this.sideQuote(p.windowId, p.side, book);
        this.deps.logger.warn({ positionId: p.id, local: oldQty, remote: rq, bid: q.bid }, "live reconcile: quantity differs");
        let detail: string;
        if (rq < oldQty) {
          const gone = oldQty - rq;
          const pnl = q.bid == null ? 0 : round((gone * (q.bid - p.avgCents)) / 100, 6);
          this.store.addPnl({ ts: now, day: etDay(now), mode: "live", windowId: p.windowId, positionId: p.id, kind: "reconcile", amountUsd: pnl });
          p.realizedPnlUsd = round(p.realizedPnlUsd + pnl, 6);
          detail =
            q.bid == null
              ? `${fmtQty(gone)} gone with no bid to price them: P&L unknown, booked $0.00`
              : `${fmtQty(gone)} gone, booked ${usd(pnl)} at the ${q.bid}¢ bid (estimate)`;
        } else {
          const added = rq - oldQty;
          const px = r?.avgCents ?? q.bid ?? q.ask ?? p.avgCents;
          if (r?.avgCents == null) p.avgCents = round((p.costUsd * 100 + added * px) / rq, 6);
          detail = `${fmtQty(added)} added${r?.avgCents == null ? ` at ~${round(px, 1)}¢ (estimate)` : ""}`;
        }
        if (rq <= EPS) {
          p.qty = 0;
          p.costUsd = 0;
          p.status = "closed";
          p.closedTs = now;
          this.closeBracketOf(p, ch);
        } else {
          p.qty = rq;
          if (r?.avgCents != null) p.avgCents = r.avgCents;
          p.costUsd = round((p.qty * p.avgCents) / 100, 6);
        }
        p.updatedTs = now;
        this.store.updatePosition(p);
        ch.position(p);
        ch.notices.push(
          this.notice("warn", "Position reconciled", `${sideName(p.side)}: local ${fmtQty(oldQty)} → exchange ${fmtQty(rq)}; ${detail}`),
        );
      }
      for (const r of remote) {
        if (seen.has(r.instrument) || !(r.qty > EPS)) continue;
        const w = this.windowForInstrument(r.instrument);
        if (!w || w.result) continue;
        const side: Side = w.upInstrument === r.instrument ? "up" : "down";
        if (busy(w.id, side)) continue; // a buy in flight: its fill creates the position
        const q = this.sideQuote(w.id, side, book);
        const avg = r.avgCents ?? q.bid ?? q.ask;
        if (avg == null) {
          if (!this.unpricedNotified.has(r.instrument)) {
            this.unpricedNotified.add(r.instrument);
            ch.notices.push(
              this.notice(
                "error",
                "Exchange position not adopted",
                `${sideName(side)} ${fmtQty(r.qty)} on the exchange has no average price and no quote to price it; it is adopted once one is available.`,
              ),
            );
          }
          continue;
        }
        this.unpricedNotified.delete(r.instrument);
        const estimated = r.avgCents == null;
        const p: Position = {
          id: randomUUID(),
          mode: "live",
          windowId: w.id,
          instrument: r.instrument,
          side,
          qty: round(r.qty, 6),
          avgCents: avg,
          costUsd: round((r.qty * avg) / 100, 6),
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
        this.store.insertPosition(p);
        // the entry's fee is unknown (we never saw the fill)
        this.store.addPnl({ ts: now, day: etDay(now), mode: "live", windowId: w.id, positionId: p.id, kind: "fee", amountUsd: 0, feeUnknown: true });
        ch.position(p);
        ch.notices.push(
          this.notice(
            "warn",
            "Position found on the exchange",
            `${sideName(side)} ${fmtQty(p.qty)} @ ${round(avg, 1)}¢${estimated ? " (estimated at the market: the exchange gave no average)" : ""} added, with no bracket`,
          ),
        );
      }
    });
    this.flush(ch);
  }

  private windowForInstrument(instrument: string): MarketWindow | null {
    const row = this.deps.db.raw
      .prepare("SELECT id FROM windows WHERE up_instrument = ? OR down_instrument = ? ORDER BY start_ts DESC LIMIT 1")
      .get(instrument, instrument) as { id: string } | undefined;
    return row ? this.windowOf(row.id, true) : null;
  }

  // =================================================================== fills -> orders / positions

  /** Submit + record the executor's answer; the order counts as in flight (not orphaned) until then. */
  private async execute(executor: Executor, order: Order, ctx: ExecContext, refCents: number | null, ch: Changes): Promise<void> {
    this.inFlight.add(order.id);
    try {
      const res = await this.submit(executor, order, ctx);
      this.applyExecResult(order, res, refCents, ch);
    } finally {
      this.inFlight.delete(order.id);
    }
  }

  private async submit(executor: Executor, order: Order, ctx: ExecContext): Promise<ExecResult> {
    try {
      return await executor.submit(order, ctx);
    } catch (err) {
      this.deps.logger.error({ err, orderId: order.id, mode: order.mode }, "order submit failed");
      return { externalId: null, status: "rejected", fills: [], error: (err as Error).message || "submit failed" };
    }
  }

  private applyExecResult(order: Order, res: ExecResult, refCents: number | null, ch: Changes): void {
    this.store.tx(() => {
      if (res.externalId) order.externalId = res.externalId;
      for (const f of res.fills) this.recordFill(order, f, refCents, ch);
      order.status = this.finalStatus(order, res.status);
      if (res.error) order.error = res.error;
      // a marketable buy is a USD budget: once spent, what it bought is the order's size
      if (order.action === "buy" && order.type === "marketable" && order.status === "filled") order.qty = order.filledQty;
      order.updatedTs = this.now();
      this.store.updateOrder(order);
      ch.order(order);
    });
    if (order.externalId) {
      const held = this.heldExternal.get(order.externalId);
      if (held) {
        this.heldExternal.delete(order.externalId);
        this.store.tx(() => {
          for (const u of held.updates) this.applyUpdate(order, u, ch);
        });
      }
    }
  }

  private finalStatus(o: Order, reported: OrderStatus): OrderStatus {
    if (reported === "filled" || reported === "cancelled" || reported === "rejected") {
      return reported === "rejected" && o.filledQty > 0 ? "cancelled" : reported;
    }
    if (o.filledQty >= o.qty - EPS && o.filledQty > 0) return "filled";
    return o.filledQty > 0 ? "partial" : reported;
  }

  /** Apply one execution to its order and position, write the fill and the P&L ledger. Runs inside tx(). */
  private recordFill(order: Order, f: ExecFill, refCents: number | null, ch: Changes): Fill | null {
    if (!(f.qty > 0) || !(f.priceCents >= 0)) return null;
    if (f.externalId && this.store.hasExternalFill(order.id, f.externalId)) return null;
    const now = this.now();
    const p = f.priceCents;
    const fee = f.feeUsd ?? 0;
    let q = f.qty;
    let pos: Position | null = order.positionId ? this.store.position(order.positionId) : null;
    let opened = false;

    if (order.action === "buy") {
      if (!pos || pos.status !== "open") pos = this.store.openPositionFor(order.mode, order.windowId, order.side);
      if (!pos) {
        pos = {
          id: randomUUID(),
          mode: order.mode,
          windowId: order.windowId,
          instrument: order.instrument,
          side: order.side,
          qty: 0,
          avgCents: 0,
          costUsd: 0,
          realizedPnlUsd: 0,
          feesUsd: 0,
          status: "open",
          markCents: null,
          unrealizedPnlUsd: null,
          bracketId: null,
          openedTs: f.ts || now,
          updatedTs: now,
          closedTs: null,
        };
        this.store.insertPosition(pos);
        opened = true;
      }
      pos.qty = round(pos.qty + q, 6);
      pos.costUsd = round(pos.costUsd + (q * p) / 100, 6);
      pos.avgCents = round((pos.costUsd * 100) / pos.qty, 6);
      pos.feesUsd = round(pos.feesUsd + fee, 6);
      pos.realizedPnlUsd = round(pos.realizedPnlUsd - fee, 6);
      if (fee !== 0 || f.feeUsd == null) {
        this.store.addPnl({ ts: now, day: etDay(now), mode: order.mode, windowId: order.windowId, positionId: pos.id, kind: "fee", amountUsd: -fee, feeUnknown: f.feeUsd == null });
      }
    } else if (pos && pos.status === "open") {
      q = Math.min(q, pos.qty); // never sell more than is held (reconcile fixes any live drift)
      const gross = (q * (p - pos.avgCents)) / 100;
      pos.realizedPnlUsd = round(pos.realizedPnlUsd + gross - fee, 6);
      pos.feesUsd = round(pos.feesUsd + fee, 6);
      pos.qty = round(pos.qty - q, 6);
      if (pos.qty <= EPS) {
        pos.qty = 0;
        pos.costUsd = 0;
        pos.status = "closed";
        pos.closedTs = now;
        this.closeBracketOf(pos, ch);
      } else {
        pos.costUsd = round((pos.qty * pos.avgCents) / 100, 6);
      }
      this.store.addPnl({ ts: now, day: etDay(now), mode: order.mode, windowId: order.windowId, positionId: pos.id, kind: "exit", amountUsd: round(gross - fee, 6), feeUnknown: f.feeUsd == null });
    } else {
      this.deps.logger.warn({ orderId: order.id, positionId: order.positionId }, "sell fill without an open position");
      pos = null;
    }
    if (!(q > 0)) return null;

    const fill: Fill = {
      id: randomUUID(),
      orderId: order.id,
      externalId: f.externalId,
      mode: order.mode,
      windowId: order.windowId,
      instrument: order.instrument,
      side: order.side,
      action: order.action,
      priceCents: p,
      qty: round(q, 6),
      feeUsd: f.feeUsd,
      slippageCents: refCents == null ? null : round(order.action === "buy" ? p - refCents : refCents - p, 4),
      ts: f.ts || now,
    };
    this.store.insertFill(fill);
    ch.fills.push(fill);

    const filled = round(order.filledQty + q, 6);
    order.avgFillCents = round(((order.avgFillCents ?? 0) * order.filledQty + p * q) / filled, 6);
    order.filledQty = filled;
    order.feesUsd = round(order.feesUsd + fee, 6);
    order.updatedTs = now;
    if (pos) {
      order.positionId = pos.id;
      pos.updatedTs = now;
      this.store.updatePosition(pos);
      // the entry's bracket attaches with its first fill (a resting limit may fill much later)
      if (order.action === "buy") {
        const meta = this.store.orderMeta(order.id);
        if (meta.bracket) {
          this.store.setOrderMeta(order.id, { bracket: null, refCents: meta.refCents });
          try {
            this.applyBracket(pos, meta.bracket, ch);
          } catch (err) {
            this.deps.logger.warn({ err: (err as Error).message, positionId: pos.id }, "entry bracket not attached");
          }
        }
      }
      ch.position(pos);
      if (opened) this.deps.logger.info({ positionId: pos.id, side: pos.side, windowId: pos.windowId }, "position opened");
    }
    return fill;
  }

  /** The position is flat: its OCO bracket is finished (triggered -> done, active -> cancelled). */
  private closeBracketOf(pos: Position, ch: Changes): void {
    if (!pos.bracketId) return;
    const b = this.store.bracket(pos.bracketId);
    if (!b || (b.status !== "active" && b.status !== "triggered")) return;
    b.status = b.status === "triggered" ? "done" : "cancelled";
    b.updatedTs = this.now();
    this.saveBracket(b, ch);
  }

  // =================================================================== helpers

  private executorFor(mode: TradingMode, purpose: "entry" | "exit" = "entry"): Executor {
    if (mode === "paper") return this.paper;
    const ex = this.deps.services.live.executor();
    if (ex) return ex;
    if (purpose === "exit") {
      throw new ApiError(
        409,
        "live_unavailable",
        "the live executor is unavailable (settings mode is paper, or no API keys): this LIVE position / order cannot be reached from here; switch back to live mode or manage it on Polymarket",
      );
    }
    throw new ApiError(409, "live_unavailable", "live trading is not available (keys / live mode)");
  }

  /**
   * Settings changed. Leaving live mode while live positions or orders are open: warn, because (unless the live
   * module keeps its executor available) their brackets, Close and the kill switch can no longer reach the exchange.
   */
  /**
   * Engine settings guard: leaving live mode is refused while a live position of a window that is still trading
   * is open, because its bracket, Close and the kill switch need the live executor (null in paper mode).
   * Positions of ended windows only wait for settlement and do not block.
   */
  modeSwitchBlocker(next: { mode: TradingMode }, prev: { mode: TradingMode }): string | null {
    if (prev.mode !== "live" || next.mode === "live") return null;
    const now = this.now();
    const cur = this.deps.engine.currentWindow();
    const n = this.store
      .openPositions()
      .filter((p) => p.mode === "live" && p.qty > 0 && cur != null && p.windowId === cur.id && now < cur.endTs).length;
    if (!n) return null;
    return `${n} live position${n === 1 ? " is" : "s are"} still open in the current window: close ${n === 1 ? "it" : "them"} (or wait for the window to end) before switching to paper; brackets, Close and the kill switch need live mode`;
  }

  onSettings(s: { mode: TradingMode }): void {
    const prev = this.lastMode;
    this.lastMode = s.mode;
    if (prev !== "live" || s.mode === "live") return;
    const pos = this.store.openPositions().filter((p) => p.mode === "live").length;
    const ord = this.store.workingOrders().filter((o) => o.mode === "live").length;
    if (!pos && !ord) return;
    const what = [pos && `${pos} live position${pos === 1 ? "" : "s"}`, ord && `${ord} working live order${ord === 1 ? "" : "s"}`].filter(Boolean).join(" and ");
    const reachable = this.deps.services.live.executor() != null;
    const ch = new Changes();
    ch.notices.push(
      this.notice(
        reachable ? "warn" : "error",
        "Live positions still open",
        reachable
          ? `Switched to paper with ${what}. They are still managed on the exchange (brackets, Close, kill switch).`
          : `Switched to paper with ${what}. Their brackets, Close and the kill switch cannot reach the exchange until you switch back to live.`,
      ),
    );
    this.flush(ch);
  }

  /** Startup: remember the mode so onSettings() can tell live -> paper. */
  initMode(): void {
    this.lastMode = this.deps.engine.getSettings().mode;
  }

  /**
   * Insert a new order row; returns the existing order instead when its clientOrderId already has one (the
   * UNIQUE index caught a concurrent request with the same key).
   */
  private insertOnce(order: Order, insert: () => void): Order | null {
    try {
      insert();
      return null;
    } catch (err) {
      if (order.clientOrderId && isUniqueViolation(err)) {
        const dup = this.store.orderByClientId(order.clientOrderId);
        if (dup) return dup;
      }
      throw err;
    }
  }

  private async freshBook(win: MarketWindow): Promise<BookLevels | null> {
    let b: BookLevels | null = null;
    try {
      b = await this.deps.engine.refreshBook();
    } catch (err) {
      this.deps.logger.debug({ err: (err as Error).message }, "book refresh failed");
      b = this.deps.engine.currentBook();
    }
    if (!b || b.windowId !== win.id || this.now() - b.ts > BOOK_MAX_AGE_MS) return null;
    return b;
  }

  private windowOf(id: string, refresh = false): MarketWindow | null {
    const cached = this.windowCache.get(id);
    if (cached?.result) return cached; // a settlement is final
    const cur = this.deps.engine.currentWindow();
    if (cur?.id === id) return cur;
    if (cached && !refresh) return cached;
    const w = this.deps.db.getWindow(id);
    if (w) this.windowCache.set(id, w);
    if (this.windowCache.size > 500) this.windowCache.delete(this.windowCache.keys().next().value!);
    return w;
  }

  /** Map a finished submit to the REST outcome: rejected / nothing filled -> an error after recording it. */
  private raiseForResult(order: Order, mode: TradingMode): void {
    if (order.status === "rejected") {
      if (mode === "live") throw new ApiError(502, "exchange_error", order.error ?? "order rejected by the exchange");
      throw new ApiError(409, "no_market", order.error ?? "order rejected");
    }
    if (order.type === "marketable" && order.status === "cancelled" && order.filledQty <= 0) {
      throw new ApiError(409, "no_market", order.error ?? "nothing filled within the slippage cap");
    }
  }

  private response(order: Order): PlaceOrderResponse {
    const o = this.store.order(order.id) ?? order;
    const pos = o.positionId ? this.store.position(o.positionId) : null;
    return {
      order: o,
      fills: this.store.fillsForOrder(o.id),
      position: pos ? this.withMark(pos) : null,
      bracket: pos?.bracketId ? this.store.bracket(pos.bracketId) : null,
    };
  }

  private notice(level: Notice["level"], title: string, message: string): Notice {
    return { id: randomUUID(), ts: this.now(), level, title, message };
  }

  private logOrder(o: Order): void {
    this.deps.logger.info(
      { orderId: o.id, mode: o.mode, side: o.side, action: o.action, type: o.type, reason: o.reason, limit: o.limitCents, qty: o.qty, filled: o.filledQty, avg: o.avgFillCents, status: o.status, error: o.error },
      "order",
    );
  }

  private refreshRisk(): void {
    try {
      this.deps.services.risk.refresh();
    } catch (err) {
      this.deps.logger.warn({ err }, "risk refresh failed");
    }
  }

  private flush(ch: Changes): void {
    if (ch.empty) return;
    const b = this.deps.broadcast;
    if (ch.orders.size) b({ type: "orders", data: [...ch.orders.values()] });
    if (ch.fills.length) b({ type: "fills", data: ch.fills });
    if (ch.positions.size) {
      const ps = [...ch.positions.values()].map((p) => this.withMark(p));
      for (const p of ps) this.lastMarks.set(p.id, p.markCents);
      b({ type: "positions", data: ps });
    }
    if (ch.brackets.size) b({ type: "brackets", data: [...ch.brackets.values()] });
    for (const n of ch.notices) b({ type: "notice", data: n });
    if (ch.fills.length || ch.positions.size || ch.orders.size) this.refreshRisk();
  }
}

const clampLimit = (n: number | undefined) => Math.min(1000, Math.max(1, Math.floor(n ?? 200)));

/** USD a working buy can still spend. */
function restingCost(o: Order): number {
  const left = Math.max(0, o.qty - o.filledQty);
  if (o.type === "marketable") {
    const spent = (o.filledQty * (o.avgFillCents ?? 0)) / 100;
    return Math.max(0, (o.notionalUsd ?? (left * (o.limitCents ?? 0)) / 100) - spent);
  }
  return (left * (o.limitCents ?? 0)) / 100;
}
