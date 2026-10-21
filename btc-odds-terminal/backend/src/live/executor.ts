// LiveExecutor: the exchange side of the Executor contract (same interface as trading's PaperExecutor), so the
// trading module routes an order to paper or live by settings.mode alone.
//
// Order mapping (see docs/POLYMARKET_US.md "Trading endpoints"):
//  - instrument "<slug>:long|short" -> marketSlug + intent ORDER_INTENT_{BUY,SELL}_{LONG,SHORT};
//  - "limit"      -> ORDER_TYPE_LIMIT, TIME_IN_FORCE_GOOD_TILL_CANCEL at limitCents;
//  - "marketable" -> ORDER_TYPE_LIMIT, TIME_IN_FORCE_IMMEDIATE_OR_CANCEL at limitCents (ask + maxSlippage for a buy,
//    bid - maxSlippage for a sell, computed by trading): a hard price cap, never an unbounded market order;
//  - synchronousExecution: true so IOC executions come back in the response; reason manual/close -> MANUAL,
//    bracket legs / kill -> AUTOMATIC (manualOrderIndicator);
//  - quantity rounded DOWN to cfg.qtyStep, price to cfg.tickCents (buy down, sell up: never worse than asked).
//
// Fills: the exchange reports executions on the POST response, on the private WebSocket and in GET /v1/order/{id}.
// All three go through one per-order ledger keyed by the exchange order id that dedupes by CUMULATIVE filled
// quantity (cumQuantity), so an execution seen twice (response + socket, or socket + poll) is applied once.
// Socket updates for an order whose POST is still in flight are held and merged into that submit's result.
//
// Ambiguous submits (timeout / network / 5xx / a 2xx without a usable body): the order may exist, and the API has no
// client order id, so it is NEVER re-sent and NEVER reported as rejected. When the server answered (5xx / bad body) one
// open-orders sweep runs inline; otherwise submit returns at once with status "pending" under an alias externalId
// ("unconfirmed:<local id>") and the outcome is resolved in the background over ~30 s:
//   1. an order the socket / snapshot showed for the same market + intent + price + quantity since the submit;
//   2. GET /v1/orders/open (a resting GTC);
//   3. GET /v1/portfolio/positions vs the terminal's own position for the instrument (a filled IOC is never open):
//      the same delta on two reads >= CONFIRM_GAP_MS apart is booked as the fill, a confirmed zero delta as nothing.
// Every later update for that order is reported under the alias, so trading's pending order is the one that fills.
// While unresolved: new orders for the same instrument + action are refused (no accidental doubling), busy() stays
// true (position reconcile waits instead of importing the fill as a bracket-less "found" position) and, when the
// budget runs out, a warning notice is raised and positions are reconciled.

import type { Notice, Order, OrderStatus } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";
import type { ExecContext, ExecFill, ExecResult, Executor, ExternalOrderUpdate } from "../modules/types.ts";
import { parseInstrument } from "../polymarket/discovery.ts";
import { ApiError } from "../server/errors.ts";
import type { LiveConfig } from "./config.ts";
import { LiveHttpError, type LiveRestClient } from "./rest.ts";
import {
  TERMINAL,
  intentOf,
  intentSide,
  mapExecutionType,
  mapOrderState,
  usd,
  type CreateOrderBody,
  type InstSide,
  type WireExecution,
  type WireOrder,
} from "./wire.ts";

export interface LiveExecutorDeps {
  rest: LiveRestClient;
  cfg: Pick<LiveConfig, "shortPriceQuote" | "qtyStep" | "tickCents">;
  logger: Logger;
  now: () => number;
  /** async order/fill updates (socket, polls) -> trading.applyExternal */
  emit(u: ExternalOrderUpdate): void;
  /** positions may have drifted (ambiguous submit): reconcile soon */
  requestReconcile(reason: string): void;
  /** private socket is up (polls are only needed while it is not) */
  streamConnected(): boolean;
  /** the terminal's open live position for an instrument (baseline to resolve an ambiguous submit); none = flat */
  localPosition?(instrument: string): { qty: number; costUsd: number } | null;
  /** user-facing notice */
  notify?(level: Notice["level"], title: string, message: string): void;
  /** test seams for the ambiguous-submit resolution (ms after the submit) */
  resolve?: { scheduleMs?: number[]; budgetMs?: number; confirmGapMs?: number; zeroAfterMs?: number };
}

type Action = "buy" | "sell";

interface Tracked {
  externalId: string;
  /** id trading knows this order by when it differs (an adopted ambiguous submit: "unconfirmed:<local id>") */
  alias: string | null;
  slug: string;
  instSide: InstSide;
  action: Action | null;
  intent: string | null;
  /** IOC (marketable): never rests, so polls until terminal */
  ioc: boolean;
  /** requested quantity / limit as first seen (matching an ambiguous submit) */
  qty: number | null;
  priceExCents: number | null;
  /** cumulative filled contracts applied so far */
  filledQty: number;
  /** sum(qty * price) of applied fills, cents in exchange quote terms */
  notionalCents: number;
  /** cumulative commission accounted for, USD */
  commissionUsd: number;
  /** settings fee override at submit, cents/contract (fallback when the exchange reports no commission) */
  feeCentsPerContract: number | null;
  limitExCents: number | null;
  status: OrderStatus;
  error: string | null;
  seenExecs: Set<string>;
  firstSeenTs: number;
  updatedTs: number;
  /** consecutive GET /v1/order/{id} 404s */
  misses: number;
  pollTries: number;
  /** submitted by this terminal (or seeded from trading) */
  ours: boolean;
}

/** An ambiguous submit whose outcome is not known yet (trading holds it as "pending" under `alias`). */
interface Unresolved {
  alias: string;
  instrument: string;
  slug: string;
  instSide: InstSide;
  action: Action;
  ioc: boolean;
  body: CreateOrderBody;
  exPrice: number;
  /** limit in side cents (what trading uses) */
  sidePrice: number;
  qty: number;
  started: number;
  feeCentsPerContract: number | null;
  step: number;
  last: { delta: number; costDelta: number | null; ts: number } | null;
  running: boolean;
  timer: NodeJS.Timeout | null;
}

export const UNCONFIRMED_PREFIX = "unconfirmed:";

const EPS = 1e-6;
const QUIET_AFTER_ACTIVITY_MS = 3000;
const PRUNE_TERMINAL_MS = 10 * 60_000;
const POLL_STALE_MS = 5000;
const MAX_POLL_TRIES = 12;
/** a matching exchange order may be created a little before our clock says the POST went out */
const ADOPT_SLACK_MS = 10_000;
const RESOLVE_SCHEDULE_MS = [1000, 2500, 5000, 9000, 15_000, 22_000, 30_000];
const RESOLVE_BUDGET_MS = 30_000;
const CONFIRM_GAP_MS = 2000;
const ZERO_AFTER_MS = 8000;
const FILL_TYPES = new Set(["EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"]);
/** implied fill prices may miss the limit by float noise only */
const PRICE_SLACK_CENTS = 0.5;

const roundQty = (q: number) => Math.round(q * 1e6) / 1e6;
const roundUsd = (v: number) => Math.round(v * 1e6) / 1e6;
const actionOf = (intent: string | null | undefined): Action | null =>
  typeof intent === "string" ? (intent.includes("_BUY_") ? "buy" : intent.includes("_SELL_") ? "sell" : null) : null;

export class LiveExecutor implements Executor {
  readonly mode = "live" as const;
  private readonly orders = new Map<string, Tracked>();
  private readonly aliasToReal = new Map<string, string>();
  private readonly unresolved = new Map<string, Unresolved>();
  private readonly held = new Map<string, ExternalOrderUpdate[]>();
  private readonly timers = new Set<NodeJS.Timeout>();
  private readonly pollTimers = new Map<string, NodeJS.Timeout>();
  private readonly lastFillBySlug = new Map<string, number>();
  private readonly notifiedUntracked = new Set<string>();
  private inflight = 0;
  private lastActivityTs = 0;
  private snapshotIds: Set<string> | null = null;
  private stopped = false;
  private readonly resolveSchedule: number[];
  private readonly resolveBudgetMs: number;
  private readonly confirmGapMs: number;
  private readonly zeroAfterMs: number;

  constructor(private readonly d: LiveExecutorDeps) {
    this.resolveSchedule = d.resolve?.scheduleMs ?? RESOLVE_SCHEDULE_MS;
    this.resolveBudgetMs = d.resolve?.budgetMs ?? RESOLVE_BUDGET_MS;
    this.confirmGapMs = d.resolve?.confirmGapMs ?? CONFIRM_GAP_MS;
    this.zeroAfterMs = d.resolve?.zeroAfterMs ?? ZERO_AFTER_MS;
  }

  // ---------------------------------------------------------------- price conversion

  /** side cents (what the UI and trading use) <-> exchange quote cents; an involution. */
  private toEx(cents: number, side: InstSide): number {
    return side === "short" && this.d.cfg.shortPriceQuote === "long" ? Math.round((100 - cents) * 1e6) / 1e6 : cents;
  }

  private floorQty(q: number): number {
    const step = this.d.cfg.qtyStep;
    return roundQty(Math.floor(q / step + 1e-9) * step);
  }

  private tickPrice(cents: number, action: Action): number {
    const t = this.d.cfg.tickCents;
    const r = action === "buy" ? Math.floor(cents / t + 1e-9) * t : Math.ceil(cents / t - 1e-9) * t;
    return Math.min(99, Math.max(1, Math.round(r * 10) / 10));
  }

  // ---------------------------------------------------------------- Executor

  async submit(order: Order, ctx: ExecContext): Promise<ExecResult> {
    const reject = (error: string): ExecResult => ({ externalId: null, status: "rejected", fills: [], error });
    if (this.stopped) return reject("live trading is not active");
    const w = ctx.window;
    if (order.instrument !== w.upInstrument && order.instrument !== w.downInstrument) {
      return reject(`instrument ${order.instrument} is not traded in window ${w.id}`);
    }
    if (this.d.now() >= w.endTs) return reject("the window has ended");
    if (order.limitCents == null || !Number.isFinite(order.limitCents)) return reject("a limit price is required for live orders");
    for (const u of this.unresolved.values()) {
      if (u.instrument === order.instrument && u.action === order.action) {
        return reject(`the previous live ${u.action} on this side is still being confirmed with the exchange (up to ${Math.round(this.resolveBudgetMs / 1000)} s); try again shortly`);
      }
    }

    const inst = parseInstrument(order.instrument);
    const price = this.tickPrice(order.limitCents, order.action);
    let want = order.qty - (order.filledQty ?? 0);
    if (!(want > 0) && order.action === "buy" && order.notionalUsd) want = order.notionalUsd / (price / 100);
    const qty = this.floorQty(want);
    if (!(qty > 0)) return reject(`quantity rounds to 0 contracts (exchange step ${this.d.cfg.qtyStep})`);

    const exPrice = this.toEx(price, inst.side);
    const ioc = order.type === "marketable";
    const body: CreateOrderBody = {
      marketSlug: inst.slug,
      intent: intentOf(order.action, inst.side),
      type: "ORDER_TYPE_LIMIT",
      price: usd(exPrice),
      quantity: qty,
      tif: ioc ? "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" : "TIME_IN_FORCE_GOOD_TILL_CANCEL",
      manualOrderIndicator: order.reason === "manual" || order.reason === "close" ? "MANUAL_ORDER_INDICATOR_MANUAL" : "MANUAL_ORDER_INDICATOR_AUTOMATIC",
      synchronousExecution: true,
    };

    const started = this.d.now();
    this.inflight++;
    this.lastActivityTs = started;
    let externalId: string | null = null;
    try {
      let executions: WireExecution[] = [];
      let adopted: WireOrder | null = null;
      try {
        const res = await this.d.rest.createOrder(body);
        externalId = res.id;
        executions = res.executions;
      } catch (err) {
        const msg = (err as Error).message;
        // only a definite exchange answer (4xx) is a rejection; anything else (incl. a non-HTTP error) may have placed it
        if (err instanceof LiveHttpError && !err.ambiguous) {
          this.d.logger.warn({ err: msg, slug: inst.slug, intent: body.intent }, "live: order rejected");
          return reject(err.status === 429 ? `rate limited by the exchange: ${msg}` : msg);
        }
        // the server answered (5xx / unusable 2xx): one quick sweep of the open orders. After a timeout / network
        // error the exchange is likely unreachable, so do not double the wait: resolve in the background instead.
        const answered = err instanceof LiveHttpError && err.status !== 0 && err.status !== 408;
        if (answered) adopted = await this.adopt(body, started).catch(() => null);
        if (!adopted) {
          this.d.logger.error({ err: msg, slug: inst.slug, intent: body.intent }, "live: order outcome unknown; resolving");
          const alias = this.unconfirmed(order, inst.slug, inst.side, body, exPrice, price, qty, started, ctx.feeCentsPerContract);
          return { externalId: alias, status: "pending", fills: [], error: `the exchange did not confirm the order (${msg}); confirming its outcome` };
        }
        externalId = adopted.id;
        this.d.logger.warn({ externalId, err: msg }, "live: adopted an order after an ambiguous submit");
      }

      const t = this.track(externalId, inst.slug, inst.side, true);
      this.claim(t, { action: order.action, intent: body.intent, ioc, qty, exPrice, fee: ctx.feeCentsPerContract });
      if (t.status === "pending" && executions.length === 0 && !adopted) t.status = ioc ? "pending" : "open";

      const fills: ExecFill[] = [];
      for (const u of this.held.get(externalId) ?? []) if (u.fill) fills.push(u.fill);
      this.held.delete(externalId);
      for (const ex of executions) {
        const o = ex.order ?? this.stubOrder(externalId, inst.slug, body.intent);
        for (const u of this.ingest(o.id === externalId ? o : { ...o, id: externalId }, ex)) if (u.fill) fills.push(u.fill);
      }
      if (adopted) for (const u of this.ingest(adopted, null)) if (u.fill) fills.push(u.fill);
      if (fills.length > 0) this.lastActivityTs = this.d.now();
      if (!TERMINAL.has(t.status)) this.schedulePoll(externalId, ioc ? 1000 : 3000);
      return { externalId, status: t.status, fills, error: t.status === "rejected" ? t.error ?? "rejected by the exchange" : null };
    } finally {
      this.inflight--;
      if (this.inflight === 0) this.flushHeld();
    }
  }

  async cancel(order: Order): Promise<void> {
    if (!order.externalId) return;
    let id = order.externalId;
    if (id.startsWith(UNCONFIRMED_PREFIX)) {
      const real = this.aliasToReal.get(id);
      if (!real) {
        const u = this.unresolved.get(id);
        if (!u) return; // nothing known on the exchange
        void this.resolveStep(u);
        throw new ApiError(502, "exchange_error", "the order is still being confirmed with the exchange; try again in a few seconds");
      }
      id = real;
    }
    const { slug } = parseInstrument(order.instrument);
    try {
      await this.d.rest.cancelOrder(id, slug);
    } catch (err) {
      if (err instanceof LiveHttpError && err.status === 404) {
        // not cancellable: already cancelled, filled or expired. Learn which before reporting success, so a missed
        // fill reaches trading before it marks the order cancelled.
        const st = await this.pollOrder(id, { rearm: false });
        if (st === null) {
          // unknown to the exchange as well: trading marks it cancelled; any fill shows up in the positions reconcile
          const t = this.orders.get(id);
          if (t && !TERMINAL.has(t.status)) {
            t.status = "cancelled";
            t.updatedTs = this.d.now();
          }
          this.d.requestReconcile("cancel 404: order state unknown");
        } else if (!TERMINAL.has(st)) {
          this.schedulePoll(id, 1500);
          throw new ApiError(502, "exchange_error", "cancel failed: the exchange reports the order still working");
        }
        return;
      }
      this.schedulePoll(id, 0); // learn the real state (it may have filled meanwhile)
      throw new ApiError(502, "exchange_error", `cancel failed: ${(err as Error).message}`);
    } finally {
      this.lastActivityTs = this.d.now();
    }
    this.schedulePoll(id, 1500);
  }

  // ---------------------------------------------------------------- extra live operations

  /** POST /v1/orders/open/cancel (kill switch helper); returns the exchange's cancelled ids. */
  async cancelAllRemote(slugs?: string[]): Promise<string[]> {
    const ids = await this.d.rest.cancelAll(slugs);
    for (const id of ids) this.schedulePoll(id, 1000);
    return ids;
  }

  /**
   * Leaving live mode: cancel every resting order of ours (one by one: other clients' orders are left alone), learn
   * their final state (fills reach trading), give unresolved submits a last look. The caller stops the executor after.
   */
  async drain(): Promise<{ cancelled: number; failed: string[]; unknown: number }> {
    const resting = [...this.orders.values()].filter((t) => t.ours && !TERMINAL.has(t.status));
    let cancelled = 0;
    const failed: string[] = [];
    for (const t of resting) {
      try {
        await this.d.rest.cancelOrder(t.externalId, t.slug);
        cancelled++;
      } catch (err) {
        if (err instanceof LiveHttpError && err.status === 404) cancelled++;
        else failed.push(`${t.externalId}: ${(err as Error).message}`);
      }
    }
    for (const t of resting) await this.pollOrder(t.externalId, { rearm: false });
    await this.resolveUnconfirmed();
    const unknown = this.unresolved.size;
    for (const u of [...this.unresolved.values()]) this.giveUp(u, "live trading stopped");
    return { cancelled, failed, unknown };
  }

  /** a submit is in flight or unconfirmed, or a fill / cancel happened moments ago: positions may lag behind */
  busy(): boolean {
    return this.inflight > 0 || this.unresolved.size > 0 || this.d.now() - this.lastActivityTs < QUIET_AFTER_ACTIVITY_MS;
  }

  /** last fill seen for a market (any order of ours), 0 = none */
  lastFillTs(slug: string): number {
    return this.lastFillBySlug.get(slug) ?? 0;
  }

  /** Seed the ledger from trading's stored live orders (restart): fills already applied are not re-emitted. */
  seed(orders: Order[]): void {
    for (const o of orders) {
      if (o.mode !== "live" || !o.externalId || TERMINAL.has(o.status) || this.orders.has(o.externalId)) continue;
      if (o.externalId.startsWith(UNCONFIRMED_PREFIX)) {
        // an ambiguous submit from before the restart: its context is gone; positions reconcile picks up any fill
        this.d.emit({ externalId: o.externalId, status: "cancelled", error: "outcome unknown after a restart; positions are reconciled from the exchange" });
        this.d.requestReconcile("unconfirmed order after a restart");
        continue;
      }
      const inst = parseInstrument(o.instrument);
      const t = this.track(o.externalId, inst.slug, inst.side, true);
      t.filledQty = o.filledQty;
      t.notionalCents = o.avgFillCents != null ? o.filledQty * this.toEx(o.avgFillCents, inst.side) : 0;
      t.commissionUsd = o.feesUsd;
      t.status = o.status;
      t.action = o.action;
      t.ioc = o.type === "marketable";
      t.limitExCents = o.limitCents != null ? this.toEx(o.limitCents, inst.side) : null;
    }
  }

  /** private socket: one execution */
  onExecution(ex: WireExecution): void {
    if (!ex.order) return;
    this.dispatch(this.ingest(ex.order, ex));
  }

  /** private socket order snapshot frame (several frames until eof) */
  onOrderSnapshot(orders: WireOrder[], eof: boolean): void {
    this.snapshotIds ??= new Set();
    for (const o of orders) {
      this.snapshotIds.add(o.id);
      this.dispatch(this.ingest(o, null)); // tracks unknown orders too (as not ours: nothing is emitted for them)
    }
    if (!eof) return;
    const seen = this.snapshotIds;
    this.snapshotIds = null;
    // our open orders missing from the snapshot ended while we were not looking: ask for their final state
    for (const t of this.orders.values()) if (t.ours && !TERMINAL.has(t.status) && !seen.has(t.externalId)) this.schedulePoll(t.externalId, 0);
    // open orders this terminal does not know: maybe an unconfirmed submit of ours; else tell the user once
    void this.resolveUnconfirmed().finally(() => {
      for (const id of seen) {
        const t = this.orders.get(id);
        if (!t || t.ours || TERMINAL.has(t.status) || this.notifiedUntracked.has(id)) continue;
        this.notifiedUntracked.add(id);
        const what = `${t.intent ?? "order"} ${t.qty ?? "?"} @ ${t.priceExCents ?? "?"}¢ on ${t.slug || "?"}`;
        this.d.logger.warn({ externalId: id, slug: t.slug, intent: t.intent }, "live: untracked open order on the exchange");
        this.d.notify?.("warn", "Untracked live order", `${what} is working on Polymarket US but was not placed by this terminal: no bracket, not in risk.`);
      }
    });
  }

  /** GET /v1/order/{id} for our non-terminal orders (all when `force`, else the ones quiet for a while). */
  async pollOpen(force = false): Promise<void> {
    const now = this.d.now();
    for (const t of [...this.orders.values()]) {
      if (TERMINAL.has(t.status)) {
        if (now - t.updatedTs > PRUNE_TERMINAL_MS) this.forget(t);
        continue;
      }
      if (!t.ours) {
        if (now - t.updatedTs > PRUNE_TERMINAL_MS) this.forget(t); // someone else's order we stopped hearing about
        continue;
      }
      if (force || !this.d.streamConnected() || now - t.updatedTs > POLL_STALE_MS * 6) await this.pollOrder(t.externalId, { rearm: false });
    }
  }

  /** our orders still resting on the exchange, plus submits whose outcome is unconfirmed */
  openOrderCount(): number {
    let n = this.unresolved.size;
    for (const t of this.orders.values()) if (t.ours && !TERMINAL.has(t.status)) n++;
    return n;
  }

  /** one resolution attempt for every unconfirmed submit (also run by the timers) */
  async resolveUnconfirmed(): Promise<void> {
    for (const u of [...this.unresolved.values()]) await this.resolveStep(u);
  }

  stop(): void {
    this.stopped = true;
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    for (const t of this.pollTimers.values()) clearTimeout(t);
    this.pollTimers.clear();
    for (const u of this.unresolved.values()) if (u.timer) clearTimeout(u.timer);
  }

  resume(): void {
    this.stopped = false;
  }

  // ---------------------------------------------------------------- ledger

  private track(id: string, slug: string, instSide: InstSide, ours: boolean): Tracked {
    let t = this.orders.get(id);
    if (!t) {
      const now = this.d.now();
      t = {
        externalId: id,
        alias: null,
        slug,
        instSide,
        action: null,
        intent: null,
        ioc: false,
        qty: null,
        priceExCents: null,
        filledQty: 0,
        notionalCents: 0,
        commissionUsd: 0,
        feeCentsPerContract: null,
        limitExCents: null,
        status: "pending",
        error: null,
        seenExecs: new Set(),
        firstSeenTs: now,
        updatedTs: now,
        misses: 0,
        pollTries: 0,
        ours,
      };
      this.orders.set(id, t);
    }
    if (ours) t.ours = true;
    return t;
  }

  private claim(t: Tracked, c: { action: Action; intent: string; ioc: boolean; qty: number; exPrice: number; fee: number | null }): void {
    t.action = c.action;
    t.intent = c.intent;
    t.ioc = c.ioc;
    t.qty = c.qty;
    t.limitExCents = c.exPrice;
    t.priceExCents = c.exPrice;
    t.feeCentsPerContract = c.fee;
  }

  private forget(t: Tracked): void {
    this.orders.delete(t.externalId);
    if (t.alias) this.aliasToReal.delete(t.alias);
    this.notifiedUntracked.delete(t.externalId);
  }

  /** a tracked order by exchange id or by the alias trading knows it by */
  private lookup(id: string): Tracked | undefined {
    const t = this.orders.get(id);
    if (t) return t;
    const real = this.aliasToReal.get(id);
    return real ? this.orders.get(real) : undefined;
  }

  private stubOrder(id: string, slug: string, intent: string): WireOrder {
    return {
      id,
      marketSlug: slug,
      intent,
      state: null,
      priceCents: null,
      quantity: null,
      cumQuantity: null,
      leavesQuantity: null,
      avgCents: null,
      commissionUsd: null,
      createTs: null,
    };
  }

  /** Price of a fill of `delta` contracts that brings the order to `cum` (exchange quote cents), null = unknown. */
  private fillPrice(t: Tracked, o: WireOrder, ex: WireExecution | null, cum: number, delta: number): number | null {
    if (ex?.lastCents != null && ex.lastShares != null && Math.abs(ex.lastShares - delta) < EPS) return ex.lastCents;
    const limit = t.limitExCents ?? o.priceCents;
    const plausible = (px: number) => {
      if (!(px >= 0 && px <= 100)) return false;
      if (limit == null || t.action == null) return true;
      // buys never fill above the limit, sells never below (in exchange quote terms the short side under the "long"
      // quote convention is inverted: there a buy's exchange price is >= its limit)
      const inverted = t.instSide === "short" && this.d.cfg.shortPriceQuote === "long";
      const buyLike = (t.action === "buy") !== inverted;
      return buyLike ? px <= limit + PRICE_SLACK_CENTS : px >= limit - PRICE_SLACK_CENTS;
    };
    if (o.avgCents != null) {
      const implied = (cum * o.avgCents - t.notionalCents) / delta;
      if (plausible(implied)) return Math.round(implied * 100) / 100;
      this.d.logger.warn({ externalId: o.id, implied, limit: t.limitExCents }, "live: implied fill price outside the order's limit; not used");
    }
    if (ex?.lastCents != null && plausible(ex.lastCents)) return ex.lastCents;
    // the order's limit is the worst price it can have filled at; an average is only safe for a single fill
    if (limit != null) return limit;
    if (o.avgCents != null && t.filledQty <= EPS) return o.avgCents;
    return null;
  }

  /** Apply one order state (+ optional execution) to the ledger; returns the updates to report (fills first). */
  private ingest(o: WireOrder, ex: WireExecution | null): ExternalOrderUpdate[] {
    const now = this.d.now();
    let t = this.orders.get(o.id);
    if (!t) {
      const side = intentSide(o.intent) ?? "long";
      // an order we did not submit (another client, or one placed before a restart that trading never stored):
      // track it from its current fill level so nothing is double counted. While a submit is in flight it may be
      // that submit's own order, so it counts from zero and is held for the submit to claim.
      t = this.track(o.id, o.marketSlug ?? "", side, false);
      t.intent = o.intent;
      t.action = actionOf(o.intent);
      t.qty = o.quantity;
      t.priceExCents = o.priceCents;
      if (o.createTs != null) t.firstSeenTs = Math.min(t.firstSeenTs, o.createTs - this.d.rest.clockOffsetMs);
      if (this.inflight === 0) {
        t.filledQty = o.cumQuantity ?? 0;
        t.notionalCents = (o.cumQuantity ?? 0) * (o.avgCents ?? 0);
        t.commissionUsd = o.commissionUsd ?? 0;
      }
    }
    if (ex?.id) {
      if (t.seenExecs.has(ex.id)) return [];
      t.seenExecs.add(ex.id);
    }

    let cum = o.cumQuantity;
    if (cum == null && ex && ex.lastShares != null && ex.lastShares > 0 && (ex.type === null || FILL_TYPES.has(ex.type))) cum = t.filledQty + ex.lastShares;

    let fill: ExecFill | null = null;
    if (cum != null && cum > t.filledQty + EPS) {
      const delta = roundQty(cum - t.filledQty);
      const px = this.fillPrice(t, o, ex, cum, delta);
      if (px == null) {
        // never book a guessed price: leave the ledger where it is so a later poll (with a price) applies it
        this.d.logger.warn({ externalId: o.id }, "live: fill without a usable price; waiting for the order's state");
        if (t.ours) {
          this.schedulePoll(o.id, 1000);
          this.d.requestReconcile("fill without a price");
        }
      } else {
        let fee: number | null = null;
        if (ex?.commissionUsd != null) fee = ex.commissionUsd;
        else if (o.commissionUsd != null) fee = Math.max(0, o.commissionUsd - t.commissionUsd);
        else if (t.feeCentsPerContract != null) fee = (delta * t.feeCentsPerContract) / 100;
        if (o.commissionUsd != null) t.commissionUsd = Math.max(t.commissionUsd + (fee ?? 0), o.commissionUsd);
        else t.commissionUsd += fee ?? 0;

        t.filledQty = cum;
        t.notionalCents += delta * px;
        if (t.ours) this.lastFillBySlug.set(t.slug, now);
        fill = {
          externalId: ex?.id ?? `${o.id}:${cum}`,
          priceCents: this.toEx(px, t.instSide),
          qty: delta,
          feeUsd: fee === null ? null : roundUsd(fee),
          ts: ex?.ts ?? now,
        };
      }
    }

    let status = mapOrderState(o.state) ?? mapExecutionType(ex?.type) ?? t.status;
    if (fill && (status === "pending" || status === "open")) status = "partial";
    if (TERMINAL.has(t.status) && !TERMINAL.has(status)) status = t.status; // late / out-of-order frame
    // a terminal state whose fill could not be priced yet must not end the order before that fill is applied
    if (cum != null && cum > t.filledQty + EPS && TERMINAL.has(status) && !TERMINAL.has(t.status)) status = t.status;
    const error = status === "rejected" ? ex?.text ?? t.error ?? "rejected by the exchange" : null;
    const changed = status !== t.status;
    t.status = status;
    t.error = error;
    t.updatedTs = now;
    t.misses = 0;
    if (!t.ours && !this.inflight) return [];
    const reportId = t.alias ?? o.id;
    if (fill) return [{ externalId: reportId, status, fill, error }];
    return changed ? [{ externalId: reportId, status, error }] : [];
  }

  private dispatch(updates: ExternalOrderUpdate[]): void {
    for (const u of updates) {
      if (u.fill) this.lastActivityTs = this.d.now();
      // an order not known to be ours while a submit is in flight: hold it for that submit to claim
      if (this.inflight > 0 && !this.lookup(u.externalId)?.ours) {
        const list = this.held.get(u.externalId) ?? [];
        list.push(u);
        this.held.set(u.externalId, list);
        continue;
      }
      this.d.emit(u);
    }
  }

  private flushHeld(): void {
    const all = [...this.held.values()].flat();
    this.held.clear();
    for (const u of all) {
      const t = this.lookup(u.externalId);
      if (t && !t.ours) continue; // not ours after all
      this.d.emit(u);
    }
  }

  private schedulePoll(id: string, ms: number): void {
    if (this.stopped) return;
    const prev = this.pollTimers.get(id);
    if (prev) clearTimeout(prev);
    const timer = setTimeout(() => {
      this.pollTimers.delete(id);
      void this.pollOrder(id);
    }, ms);
    timer.unref?.();
    this.pollTimers.set(id, timer);
  }

  /**
   * GET /v1/order/{id} -> ledger. Returns the order's status, or null when the exchange did not return it. A 404 for
   * one of our working orders twice in a row means it is gone (an expired IOC is no longer queryable): reported
   * cancelled, with a reconcile when it had partly filled. Re-arms itself while an IOC / pending order is unresolved.
   */
  private async pollOrder(id: string, opts: { rearm?: boolean } = {}): Promise<OrderStatus | null> {
    if (this.stopped) return null;
    let result: OrderStatus | null = null;
    try {
      const o = await this.d.rest.getOrder(id);
      if (o) {
        this.dispatch(this.ingest(o, null));
        result = this.orders.get(id)?.status ?? null;
      } else {
        const t = this.orders.get(id);
        if (t && t.ours && !TERMINAL.has(t.status) && ++t.misses >= 2) {
          const partly = t.filledQty > EPS;
          t.status = "cancelled";
          t.error = partly ? "order no longer on the exchange after a partial fill; positions are reconciled" : "order no longer on the exchange";
          t.updatedTs = this.d.now();
          this.dispatch([{ externalId: t.alias ?? id, status: "cancelled", error: t.error }]);
          if (partly) this.d.requestReconcile("order vanished after a partial fill");
        }
      }
    } catch (err) {
      this.d.logger.debug({ err: (err as Error).message, externalId: id }, "live: order poll failed");
    }
    const t = this.orders.get(id);
    if (opts.rearm !== false && t && t.ours && !TERMINAL.has(t.status) && !this.stopped && (t.ioc || t.status === "pending" || t.misses > 0)) {
      if (++t.pollTries <= MAX_POLL_TRIES) this.schedulePoll(id, Math.min(15_000, 1000 * 2 ** Math.min(t.pollTries - 1, 4)));
      else this.d.requestReconcile("order state unresolved after repeated polls");
    }
    return result;
  }

  /** An open order matching `body` created since `since` that is not already one of ours. */
  private async adopt(body: CreateOrderBody, since: number): Promise<WireOrder | null> {
    const open = await this.d.rest.openOrders([body.marketSlug]);
    const hits = open.filter((o) => this.matches(body, since, { id: o.id, intent: o.intent, priceExCents: o.priceCents, qty: o.quantity, createdTs: o.createTs }));
    return hits.length === 1 ? hits[0]! : null;
  }

  /** `createdTs` is exchange time (converted with the REST clock offset); a local first-seen time is passed as is. */
  private matches(
    body: CreateOrderBody,
    since: number,
    o: { id: string; intent: string | null; priceExCents: number | null; qty: number | null; createdTs?: number | null; firstSeenTs?: number },
  ): boolean {
    if (this.orders.get(o.id)?.ours) return false;
    if (o.intent !== body.intent) return false;
    const want = body.price ? Number(body.price.value) * 100 : null;
    if (want !== null && (o.priceExCents === null || Math.abs(o.priceExCents - want) >= 0.05)) return false;
    if (o.qty !== null && body.quantity !== undefined && Math.abs(o.qty - body.quantity) >= EPS) return false;
    if (o.createdTs != null && o.createdTs - this.d.rest.clockOffsetMs < since - ADOPT_SLACK_MS) return false;
    if (o.firstSeenTs != null && o.firstSeenTs < since - ADOPT_SLACK_MS) return false;
    return true;
  }

  // ---------------------------------------------------------------- ambiguous submits

  private unconfirmed(
    order: Order,
    slug: string,
    instSide: InstSide,
    body: CreateOrderBody,
    exPrice: number,
    sidePrice: number,
    qty: number,
    started: number,
    fee: number | null,
  ): string {
    const alias = `${UNCONFIRMED_PREFIX}${order.id}`;
    const u: Unresolved = {
      alias,
      instrument: order.instrument,
      slug,
      instSide,
      action: order.action,
      ioc: order.type === "marketable",
      body,
      exPrice,
      sidePrice,
      qty,
      started,
      feeCentsPerContract: fee,
      step: 0,
      last: null,
      running: false,
      timer: null,
    };
    this.unresolved.set(alias, u);
    this.armResolve(u);
    return alias;
  }

  private armResolve(u: Unresolved): void {
    if (this.stopped || !this.unresolved.has(u.alias)) return;
    const at = this.resolveSchedule[u.step];
    if (at === undefined) return;
    u.step++;
    const wait = Math.max(0, u.started + at - this.d.now());
    u.timer = setTimeout(() => {
      u.timer = null;
      void this.resolveStep(u).finally(() => this.armResolve(u));
    }, wait);
    u.timer.unref?.();
  }

  private async resolveStep(u: Unresolved): Promise<void> {
    if (this.stopped || u.running || !this.unresolved.has(u.alias)) return;
    u.running = true;
    try {
      // 1. the order already showed up on the socket / snapshot (tracked as not ours)
      const seen = [...this.orders.values()].filter((t) => t.slug === u.slug && this.matches(u.body, u.started, { id: t.externalId, intent: t.intent, priceExCents: t.priceExCents, qty: t.qty, firstSeenTs: t.firstSeenTs }));
      if (seen.length === 1) return await this.link(u, seen[0]!.externalId, null);
      // 2. resting
      const open = await this.adopt(u.body, u.started).catch(() => null);
      if (open) return await this.link(u, open.id, open);
      if (!this.unresolved.has(u.alias)) return;
      // 3. filled (or not) per the positions
      const read = await this.positionDelta(u).catch((err: Error) => {
        this.d.logger.debug({ err: err.message, alias: u.alias }, "live: positions read for an unconfirmed order failed");
        return null;
      });
      if (!this.unresolved.has(u.alias)) return;
      const now = this.d.now();
      if (read) {
        const prev = u.last;
        u.last = { ...read, ts: now };
        if (prev && Math.abs(prev.delta - read.delta) < EPS && now - prev.ts >= this.confirmGapMs) {
          if (read.delta > EPS) return this.bookFromPositions(u, read.delta, read.costDelta);
          if (now - u.started >= this.zeroAfterMs) {
            return this.finish(
              u,
              u.ioc ? "cancelled" : "rejected",
              u.ioc ? "nothing filled (confirmed from positions after an unconfirmed submit)" : "the exchange has no record of the order (confirmed after an unconfirmed submit)",
            );
          }
        }
      }
      if (now - u.started >= this.resolveBudgetMs) this.giveUp(u, "the exchange could not be reached");
    } finally {
      u.running = false;
    }
  }

  /** contracts the unconfirmed order moved the held position by (buy: up, sell: down), clamped to its size */
  private async positionDelta(u: Unresolved): Promise<{ delta: number; costDelta: number | null }> {
    const read = await this.d.rest.positions({ market: u.slug });
    if (read.unparseable.includes(u.slug)) throw new Error(`positions: ${u.slug} has an unparseable netPosition`);
    const p = read.positions.find((x) => x.slug === u.slug);
    const heldSide = p && Math.abs(p.net) > EPS ? (p.net > 0 ? "long" : "short") : null;
    const held = p && heldSide === u.instSide ? Math.abs(p.net) : 0;
    const heldCost = held > 0 ? p!.costUsd : 0;
    const local = this.d.localPosition?.(u.instrument) ?? null;
    const lq = local?.qty ?? 0;
    const raw = u.action === "buy" ? held - lq : lq - held;
    const delta = Math.min(u.qty, Math.max(0, roundQty(raw)));
    const costDelta = u.action === "buy" && heldCost != null ? Math.abs(heldCost) - (local?.costUsd ?? 0) : null;
    return { delta, costDelta };
  }

  private bookFromPositions(u: Unresolved, delta: number, costDelta: number | null): void {
    let px = u.sidePrice; // the limit: the worst price it can have filled at
    if (costDelta != null) {
      const c = (costDelta / delta) * 100;
      if (c > 0 && c <= u.sidePrice + PRICE_SLACK_CENTS) px = Math.round(c * 100) / 100;
    }
    const now = this.d.now();
    const full = delta >= u.qty - EPS;
    this.lastFillBySlug.set(u.slug, now);
    this.lastActivityTs = now;
    this.done(u);
    this.d.emit({
      externalId: u.alias,
      status: full ? "filled" : "cancelled",
      fill: { externalId: `${u.alias}:positions`, priceCents: px, qty: delta, feeUsd: u.feeCentsPerContract != null ? roundUsd((delta * u.feeCentsPerContract) / 100) : null, ts: now },
      error: full ? null : "partly filled (confirmed from positions after an unconfirmed submit)",
    });
    this.d.logger.warn({ alias: u.alias, delta, px }, "live: unconfirmed order resolved from positions");
    this.d.notify?.("warn", "Live order confirmed from positions", `The exchange did not confirm the order in time; positions show ${delta} contract(s) ${u.action === "buy" ? "bought" : "sold"} (booked at ${px}¢${costDelta == null ? ", the limit" : ""}).`);
  }

  private finish(u: Unresolved, status: OrderStatus, error: string): void {
    this.done(u);
    this.d.emit({ externalId: u.alias, status, error });
    this.d.logger.warn({ alias: u.alias, status }, "live: unconfirmed order resolved");
  }

  private giveUp(u: Unresolved, why: string): void {
    this.done(u);
    const error = `outcome unknown (${why}); positions are reconciled from the exchange`;
    this.d.emit({ externalId: u.alias, status: "cancelled", error });
    this.d.logger.error({ alias: u.alias, why }, "live: unconfirmed order outcome still unknown");
    this.d.notify?.("warn", "Live order outcome unknown", `A live ${u.action} on ${u.slug} was not confirmed by the exchange. Check Polymarket US: any position is imported by the reconcile without a bracket.`);
    this.d.requestReconcile("unconfirmed order outcome unknown");
  }

  private done(u: Unresolved): void {
    if (u.timer) clearTimeout(u.timer);
    u.timer = null;
    this.unresolved.delete(u.alias);
  }

  /** The unconfirmed submit is exchange order `id`: report everything about it under the alias trading holds. */
  private async link(u: Unresolved, id: string, o: WireOrder | null): Promise<void> {
    if (!this.unresolved.has(u.alias)) return;
    this.done(u);
    const t = this.track(id, u.slug, u.instSide, false);
    // whatever the ledger saw of it so far was not reported: start from zero so every fill reaches trading once
    t.filledQty = 0;
    t.notionalCents = 0;
    t.commissionUsd = 0;
    t.seenExecs.clear();
    t.status = "pending";
    t.ours = true;
    t.alias = u.alias;
    this.aliasToReal.set(u.alias, id);
    this.claim(t, { action: u.action, intent: u.body.intent, ioc: u.ioc, qty: u.qty, exPrice: u.exPrice, fee: u.feeCentsPerContract });
    this.d.logger.warn({ externalId: id, alias: u.alias }, "live: unconfirmed order found on the exchange");
    const cur = o ?? (await this.d.rest.getOrder(id).catch(() => null));
    if (cur) this.dispatch(this.ingest(cur, null));
    if (!TERMINAL.has(t.status)) this.schedulePoll(id, cur ? 1000 : 0);
  }
}
