// MarketTracker: discovers the current and next "BTC Up or Down: 15 min" windows, streams/polls the Up/Down
// top of book (CENTS), rolls over at each boundary without restart and records settlement.
//
// Events: "window" (MarketWindow) on any insert/update; "rollover" ({ current, next }) at each boundary;
//         "odds" (OddsSnap, cents, windowId = current.id); "status" (FeedStatus); "settled" (MarketWindow).

import { EventEmitter } from "node:events";
import { STALE_AFTER_MS } from "../../../shared/protocol.ts";
import type { FeedState, FeedStatus, MarketWindow, OddsSnap } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";
import { PolymarketClient, type MarketDataSource, type RawEvent } from "./client.ts";
import { loadPolyConfig, type PolyConfig } from "./config.ts";
import {
  mergeWindow,
  pickCurrentNext,
  sameWindow,
  sidePrices,
  statusAt,
  windowsFromEvents,
  isBtc15mEvent,
  type DiscoveredWindow,
} from "./discovery.ts";
import { settlementToResult, type NormalizedBbo, type NormalizedBook } from "./normalize.ts";
import { MarketsStream } from "./ws.ts";

export interface TrackerOptions {
  logger: Logger;
  pollMs?: number; // BBO poll fallback interval, default 1000
  priceAt?: (ts: number) => number | null; // BRTI-proxy price at a time; PTB fallback if metadata lacks it
  /** test seams / overrides (optional) */
  client?: MarketDataSource & { getBook?(slug: string): Promise<NormalizedBook | null> };
  config?: Partial<PolyConfig>;
  now?: () => number;
  /** periodic discovery interval, default 30 000 */
  discoveryMs?: number;
}

const HOUSEKEEP_MS = 1000;
const SETTLE_FIRST_DELAY_MS = 5_000;
const SETTLE_RETRY_MS = 5_000;
const SETTLE_GIVE_UP_MS = 6 * 3600_000;
const KEEP_FINISHED_MS = 2 * 3600_000;
const PTB_FALLBACK_DELAY_MS = 1_000;
const OPEN_STATES = new Set(["MARKET_STATE_OPEN", "MARKET_STATE_PREOPEN", "OPEN", "PREOPEN"]);

interface LongQuote {
  bid: number | null;
  ask: number | null;
  ts: number;
}

export class MarketTracker extends EventEmitter {
  private readonly log: Logger;
  private readonly cfg: PolyConfig;
  private readonly client: NonNullable<TrackerOptions["client"]>;
  private readonly pollMs: number;
  private readonly discoveryMs: number;
  private readonly priceAt: TrackerOptions["priceAt"];
  private readonly now: () => number;

  private running = false;
  private timers: NodeJS.Timeout[] = [];
  private boundaryTimer: NodeJS.Timeout | null = null;
  private stream: MarketsStream | null = null;

  private windows = new Map<string, DiscoveredWindow>();
  private currentId: string | null = null;
  private nextId: string | null = null;
  private quotes = new Map<string, LongQuote>();
  private _lastOdds: OddsSnap | null = null;

  private lastDataTs: number | null = null;
  private lastError: string | null = null;
  private consecutiveErrors = 0;
  private lastStatusKey = "";
  private discovering = false;
  private lastDiscoveryAt = 0;
  private polling = false;
  private settleTries = new Map<string, number>(); // window id -> last try ts
  private settling = new Set<string>();
  private lastWarnAt = 0;

  constructor(opts: TrackerOptions) {
    super();
    this.log = opts.logger;
    this.cfg = { ...loadPolyConfig(), ...(opts.config ?? {}) };
    this.client = opts.client ?? new PolymarketClient({ baseUrl: this.cfg.gatewayUrl, timeoutMs: this.cfg.timeoutMs });
    this.pollMs = Math.max(100, opts.pollMs ?? 1000);
    this.discoveryMs = Math.max(1000, opts.discoveryMs ?? 30_000);
    this.priceAt = opts.priceAt;
    this.now = opts.now ?? Date.now;
  }

  // ------------------------------------------------------------------ public API

  start(): void {
    if (this.running) return;
    this.running = true;
    void this.discover();
    this.timers.push(setInterval(() => void this.discover(), this.discoveryMs));
    this.timers.push(setInterval(() => void this.poll(), this.pollMs));
    this.timers.push(setInterval(() => this.housekeep(), HOUSEKEEP_MS));
    if (this.wsEnabled()) {
      const auth = this.cfg.wsMode === "noauth" ? null : { keyId: this.cfg.keyId!, secretKey: this.cfg.secretKey! };
      this.stream = new MarketsStream({ url: this.cfg.wsUrl, logger: this.log, auth });
      this.stream.on("bbo", (b: NormalizedBbo) => this.onQuote(b, "ws"));
      this.stream.on("open", () => this.emitStatus(true));
      this.stream.on("close", () => this.emitStatus(true));
      this.stream.start();
    } else {
      this.log.info({ wsMode: this.cfg.wsMode }, "polymarket: markets WS needs API keys; polling BBO");
    }
    this.emitStatus(true);
  }

  stop(): void {
    this.running = false;
    for (const t of this.timers) clearInterval(t);
    this.timers = [];
    if (this.boundaryTimer) clearTimeout(this.boundaryTimer);
    this.boundaryTimer = null;
    this.stream?.stop();
    this.stream = null;
    this.emitStatus(true);
  }

  current(): MarketWindow | null {
    return this.currentId ? (this.windows.get(this.currentId)?.window ?? null) : null;
  }

  next(): MarketWindow | null {
    return this.nextId ? (this.windows.get(this.nextId)?.window ?? null) : null;
  }

  lastOdds(): OddsSnap | null {
    return this._lastOdds;
  }

  status(): FeedStatus {
    const now = this.now();
    const ageMs = this.lastDataTs === null ? null : now - this.lastDataTs;
    let state: FeedState;
    let detail: string | undefined;
    const via = this.stream?.connected ? "ws" : "poll";
    if (!this.running) {
      state = "down";
      detail = "stopped";
    } else if (this.lastDataTs === null) {
      state = "down";
      detail = this.lastError ?? (this.currentId ? "waiting for data" : "no active window");
    } else if (ageMs! > STALE_AFTER_MS) {
      state = this.consecutiveErrors >= 3 ? "down" : "stale";
      detail = this.lastError ?? (this.currentId ? `no data (${via})` : "no active window");
    } else {
      state = "live";
      detail = via;
    }
    return { id: "polymarket", state, lastMsgTs: this.lastDataTs, ageMs, detail };
  }

  /** All known windows (newest first); handy for the recorder/back-fill. */
  windowsList(): MarketWindow[] {
    return [...this.windows.values()].map((d) => d.window).sort((a, b) => b.startTs - a.startTs);
  }

  // ------------------------------------------------------------------ discovery

  private wsEnabled(): boolean {
    if (this.cfg.wsMode === "off") return false;
    if (this.cfg.wsMode === "noauth") return true;
    return !!(this.cfg.keyId && this.cfg.secretKey);
  }

  /** Fetch candidate events from every configured source; tolerate partial failure. */
  private async fetchEvents(): Promise<RawEvent[]> {
    const c = this.client;
    const jobs: Promise<RawEvent[]>[] = [
      c.searchEvents({ query: this.cfg.searchQuery, status: "active", limit: 50 }),
      c.searchEvents({ query: this.cfg.searchQuery, status: "upcoming", limit: 50 }),
    ];
    if (this.cfg.seriesIds.length > 0) {
      jobs.push(c.listEvents({ seriesId: this.cfg.seriesIds, active: true, closed: false, limit: 100 }));
    }
    if (this.cfg.tagSlug) jobs.push(c.listEvents({ tagSlug: this.cfg.tagSlug, active: true, closed: false, limit: 100 }));
    const results = await Promise.allSettled(jobs);
    const events: RawEvent[] = [];
    const errors: string[] = [];
    for (const r of results) {
      if (r.status === "fulfilled") events.push(...r.value);
      else errors.push((r.reason as Error)?.message ?? String(r.reason));
    }
    if (events.length === 0 && errors.length > 0) throw new Error(errors[0]);

    // events listed without nested markets: fetch them by slug (bounded)
    const now = this.now();
    const bySlug = new Map<string, RawEvent>();
    for (const e of events) if (typeof e.slug === "string") bySlug.set(e.slug, e);
    const missing = [...bySlug.values()]
      .filter((e) => !Array.isArray(e.markets) || e.markets.length === 0)
      .filter((e) => isBtc15mEvent({ ...e, markets: [] }, this.cfg, now))
      .slice(0, 4);
    await Promise.allSettled(
      missing.map(async (e) => {
        const full = await c.getEventBySlug(String(e.slug));
        if (full) bySlug.set(String(e.slug), full);
      }),
    );
    return [...bySlug.values()];
  }

  async discover(): Promise<void> {
    if (!this.running || this.discovering) return;
    this.discovering = true;
    this.lastDiscoveryAt = this.now();
    try {
      const events = await this.fetchEvents();
      const found = windowsFromEvents(events, this.now(), this.cfg);
      this.ingest(found);
      if (found.length === 0) this.warn("polymarket discovery: no BTC 15m windows found", { events: events.length });
    } catch (err) {
      this.noteError(`discovery: ${(err as Error).message}`);
    } finally {
      this.discovering = false;
    }
  }

  /** Merge discovered windows (also used by tests). */
  ingest(found: DiscoveredWindow[]): void {
    const now = this.now();
    for (const dw of found) {
      const prev = this.windows.get(dw.window.id);
      const merged = mergeWindow(prev, dw, now);
      this.windows.set(merged.window.id, merged);
      if (!prev || !sameWindow(prev.window, merged.window)) this.emit("window", merged.window);
    }
    this.select(now);
  }

  // ------------------------------------------------------------------ rollover

  private select(now: number): void {
    const { current, next } = pickCurrentNext([...this.windows.values()], now);
    const newCur = current?.window.id ?? null;
    const newNext = next?.window.id ?? null;
    const curChanged = newCur !== this.currentId;
    const nextChanged = newNext !== this.nextId;
    this.currentId = newCur;
    this.nextId = newNext;
    if (curChanged) {
      const keep = new Set(this.streamSlugs());
      for (const slug of [...this.quotes.keys()]) if (!keep.has(slug)) this.quotes.delete(slug);
      this._lastOdds = null;
      this.emit("rollover", { current: this.current(), next: this.next() });
      this.log.info({ current: newCur, next: newNext }, "polymarket: rollover");
    }
    if (curChanged || nextChanged) {
      this.armBoundary(now);
      this.stream?.setSlugs(this.streamSlugs());
    }
  }

  private streamSlugs(): string[] {
    const out: string[] = [];
    for (const id of [this.currentId, this.nextId]) {
      const d = id ? this.windows.get(id) : undefined;
      if (d) out.push(d.upSlug, d.downSlug);
    }
    return out;
  }

  private armBoundary(now: number): void {
    if (this.boundaryTimer) clearTimeout(this.boundaryTimer);
    this.boundaryTimer = null;
    if (!this.running) return;
    const cur = this.current();
    const nxt = this.next();
    const at = cur ? cur.endTs : nxt ? nxt.startTs : null;
    if (at === null) return;
    const delay = Math.max(0, at - now) + 20;
    if (delay > 2 ** 31 - 1) return;
    this.boundaryTimer = setTimeout(() => {
      this.boundaryTimer = null;
      this.housekeep();
      void this.discover();
    }, delay);
  }

  // ------------------------------------------------------------------ housekeeping (1 Hz)

  private housekeep(): void {
    if (!this.running) return;
    const now = this.now();
    for (const d of this.windows.values()) {
      const st = statusAt(d.window, now);
      if (st !== d.window.status) {
        d.window = { ...d.window, status: st };
        this.emit("window", d.window);
      }
    }
    this.select(now);

    // discover eagerly near/after a boundary when the next window is unknown
    const cur = this.current();
    const needSoon = !cur || (!this.nextId && cur.endTs - now < 120_000);
    if (needSoon && now - this.lastDiscoveryAt > 5_000) void this.discover();

    this.fillPtb(now);
    this.settle(now);
    this.prune(now);
    this.emitStatus(false);
  }

  private fillPtb(now: number): void {
    if (!this.priceAt) return;
    for (const d of this.windows.values()) {
      const w = d.window;
      if (w.priceToBeat !== null || d.ptbFromMetadata) continue;
      if (now < w.startTs + PTB_FALLBACK_DELAY_MS || now > w.endTs + 3600_000) continue;
      let p: number | null = null;
      try {
        p = this.priceAt(w.startTs);
      } catch {
        p = null;
      }
      if (p !== null && Number.isFinite(p) && p > 0) {
        d.window = { ...w, priceToBeat: p };
        this.log.info({ id: w.id, ptb: p }, "polymarket: PTB from BRTI proxy (metadata lacked it)");
        this.emit("window", d.window);
      }
    }
  }

  private settle(now: number): void {
    for (const d of this.windows.values()) {
      const w = d.window;
      if (w.result || now < w.endTs + SETTLE_FIRST_DELAY_MS || now > w.endTs + SETTLE_GIVE_UP_MS) continue;
      if (this.settling.has(w.id)) continue;
      const last = this.settleTries.get(w.id) ?? 0;
      if (now - last < SETTLE_RETRY_MS) continue;
      this.settleTries.set(w.id, now);
      this.settling.add(w.id);
      void this.trySettle(d).finally(() => this.settling.delete(w.id));
    }
  }

  private async trySettle(d: DiscoveredWindow): Promise<void> {
    try {
      const s = await this.client.getSettlement(d.upSlug);
      if (!s) return;
      const result = settlementToResult(s.value, d.upSide === "long");
      if (result === null) {
        this.warn("polymarket: non-binary settlement value; leaving window unsettled", { id: d.window.id, value: s.value });
        return;
      }
      // Guard against an unsettled market answering 0: a 0 counts only once the book is no longer open.
      if (s.value <= 0.01 && this.client.getBook) {
        const book = await this.client.getBook(d.upSlug).catch(() => null);
        if (book?.state && OPEN_STATES.has(book.state)) return;
      }
      const cur = this.windows.get(d.window.id);
      if (!cur || cur.window.result) return;
      cur.window = { ...cur.window, result, status: "settled" };
      this.settleTries.delete(cur.window.id);
      this.log.info({ id: cur.window.id, result, value: s.value }, "polymarket: window settled");
      this.emit("window", cur.window);
      this.emit("settled", cur.window);
    } catch (err) {
      this.log.debug({ id: d.window.id, err: (err as Error).message }, "polymarket: settlement not available yet");
    }
  }

  private prune(now: number): void {
    for (const [id, d] of this.windows) {
      if (id === this.currentId || id === this.nextId) continue;
      const w = d.window;
      const done = w.result ? w.endTs + KEEP_FINISHED_MS : w.endTs + SETTLE_GIVE_UP_MS;
      if (now > done) {
        this.windows.delete(id);
        this.settleTries.delete(id);
      }
    }
  }

  // ------------------------------------------------------------------ quotes

  private async poll(): Promise<void> {
    if (!this.running || this.polling) return;
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    if (!d) return;
    // the WS is delivering: skip the REST poll
    if (this.stream?.connected && this.lastDataTs !== null && this.now() - this.lastDataTs < 2 * this.pollMs + 1000) return;
    this.polling = true;
    try {
      const slugs = [...new Set([d.upSlug, d.downSlug])];
      const bbos = await Promise.all(slugs.map((s) => this.client.getBbo(s)));
      let any = false;
      for (let i = 0; i < slugs.length; i++) {
        const b = bbos[i];
        if (b) {
          this.storeQuote(slugs[i]!, b);
          any = true;
        }
      }
      if (!any) throw new Error("bbo: empty response");
      this.consecutiveErrors = 0;
      this.lastError = null;
      this.lastDataTs = this.now();
      this.emitOdds();
    } catch (err) {
      this.noteError(`bbo: ${(err as Error).message}`);
    } finally {
      this.polling = false;
    }
  }

  private storeQuote(slug: string, b: NormalizedBbo): void {
    this.quotes.set(slug, { bid: b.bid, ask: b.ask, ts: this.now() });
  }

  private onQuote(b: NormalizedBbo, _via: "ws"): void {
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    const slug = b.slug;
    if (!slug) return;
    this.storeQuote(slug, b);
    if (d && (slug === d.upSlug || slug === d.downSlug)) {
      this.lastDataTs = this.now();
      this.consecutiveErrors = 0;
      this.lastError = null;
      this.emitOdds();
    }
  }

  /** Pure-ish: build the OddsSnap for the current window from the stored long-side quotes. */
  private emitOdds(): void {
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    if (!d) return;
    const upQ = this.quotes.get(d.upSlug);
    const downQ = this.quotes.get(d.downSlug);
    const up = upQ ? sidePrices(upQ, d.upSide) : { bid: null, ask: null };
    const down = downQ ? sidePrices(downQ, d.downSide) : { bid: null, ask: null };
    const snap = buildOddsSnap(this.now(), d.window.id, up, down);
    this._lastOdds = snap;
    this.emit("odds", snap);
    this.emitStatus(false);
  }

  // ------------------------------------------------------------------ status / errors

  private emitStatus(force: boolean): void {
    const st = this.status();
    const key = `${st.state}|${st.detail ?? ""}`;
    if (!force && key === this.lastStatusKey) return;
    this.lastStatusKey = key;
    this.emit("status", st);
  }

  private noteError(msg: string): void {
    this.consecutiveErrors++;
    this.lastError = msg;
    this.warn(`polymarket: ${msg}`);
    this.emitStatus(false);
  }

  private warn(msg: string, extra: Record<string, unknown> = {}): void {
    const now = this.now();
    if (now - this.lastWarnAt < 60_000) {
      this.log.debug(extra, msg);
      return;
    }
    this.lastWarnAt = now;
    this.log.warn(extra, msg);
  }
}

/** Pure: assemble an OddsSnap (cents). upMid = mean of bid/ask when both exist, else whichever exists. */
export function buildOddsSnap(
  ts: number,
  windowId: string,
  up: { bid: number | null; ask: number | null },
  down: { bid: number | null; ask: number | null },
): OddsSnap {
  const upMid =
    up.bid !== null && up.ask !== null ? Math.round(((up.bid + up.ask) / 2) * 100) / 100 : (up.bid ?? up.ask ?? null);
  return { ts, windowId, upBid: up.bid, upAsk: up.ask, downBid: down.bid, downAsk: down.ask, upMid };
}
