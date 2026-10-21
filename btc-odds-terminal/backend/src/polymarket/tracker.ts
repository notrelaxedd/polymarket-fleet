// MarketTracker: discovers the current and next "BTC Up or Down: 15 min" windows, streams/polls the Up/Down
// top of book (CENTS), rolls over at each boundary without restart and records settlement.
//
// Events: "window" (MarketWindow) on any insert/update; "rollover" ({ current, next }) at each boundary;
//         "odds" (OddsSnap, cents, windowId = current.id); "status" (FeedStatus); "settled" (MarketWindow).

import { EventEmitter } from "node:events";
import { STALE_AFTER_MS } from "../../../shared/protocol.ts";
import type { FeedState, FeedStatus, MarketWindow, OddsSnap } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";
import { PolyHttpError, PolymarketClient, type MarketDataSource, type Query, type RawEvent } from "./client.ts";
import { loadPolyConfig, type PolyConfig } from "./config.ts";
import {
  eventSlugsAround,
  extractTimes,
  isBtc15mEvent,
  mergeWindow,
  parseInstrument,
  pickBtc15mSeries,
  pickCurrentNext,
  sameWindow,
  sidePrices,
  statusAt,
  windowsFromEvents,
  type DiscoveredWindow,
} from "./discovery.ts";
import { settlementToResult, type NormalizedBbo, type NormalizedBook } from "./normalize.ts";
import { backoffMs, MarketsStream } from "./ws.ts";

/** What the tracker needs from a quote stream (MarketsStream; a fake in tests). */
export type QuoteStream = EventEmitter & {
  readonly connected: boolean;
  start(): void;
  stop(): void;
  setSlugs(slugs: string[]): void;
};

export interface TrackerOptions {
  logger: Logger;
  pollMs?: number; // BBO poll fallback interval, default 1000
  priceAt?: (ts: number) => number | null; // BRTI-proxy price at a time; PTB fallback if metadata lacks it
  /**
   * Restart recovery: windows from storage (e.g. db.listWindows()). Those still unsettled and ended less than
   * SETTLE_GIVE_UP_MS ago are re-adopted on start() so their settlement is still recorded.
   */
  unsettled?: () => MarketWindow[];
  /** test seams / overrides (optional) */
  client?: MarketDataSource & { getBook?(slug: string): Promise<NormalizedBook | null> };
  config?: Partial<PolyConfig>;
  now?: () => number;
  /** periodic discovery interval, default 30 000 */
  discoveryMs?: number;
  /** quote stream to use instead of the markets WebSocket (tests) */
  stream?: QuoteStream;
}

const HOUSEKEEP_MS = 1000;
const SETTLE_FIRST_DELAY_MS = 5_000;
const SETTLE_GIVE_UP_MS = 6 * 3600_000;
/** a settlement value counts only when two reads at least this far apart agree */
const SETTLE_CONFIRM_MS = 30_000;
/** a confirmed value that contradicts the BRTI proxy, or a book in a non-terminal state, is held this long */
const SETTLE_HOLD_MAX_MS = 10 * 60_000;
/** proxy vs PTB gap (relative) below which the proxy has no opinion on the result (~$50 at $100k) */
const SETTLE_PROXY_MARGIN = 0.0005;
const KEEP_FINISHED_MS = 2 * 3600_000;
const PTB_FALLBACK_DELAY_MS = 1_000;
const OPEN_STATES = new Set(["MARKET_STATE_OPEN", "MARKET_STATE_PREOPEN", "OPEN", "PREOPEN"]);
const TERMINAL_STATES = new Set([
  "MARKET_STATE_EXPIRED",
  "MARKET_STATE_TERMINATED",
  "MARKET_STATE_CLOSED",
  "EXPIRED",
  "TERMINATED",
  "CLOSED",
]);
const POLL_BACKOFF_AFTER = 3; // consecutive BBO failures before the poll slows down
const POLL_BACKOFF_MAX_MS = 15_000;
const DISCOVERY_RETRY_MS = 5_000;
const DISCOVERY_BACKOFF_MAX_MS = 60_000;
const RATE_LIMIT_DEFAULT_MS = 10_000;
const RATE_LIMIT_MAX_MS = 5 * 60_000;
const SEARCH_LIMIT = 50;
const MAX_SEARCH_PAGES = 5;
const LIST_LIMIT = 100;
const MAX_LIST_PAGES = 5;
const SERIES_RESOLVE_MS = 30 * 60_000;
const SERIES_RETRY_MS = 5 * 60_000;
const MAX_EVENT_EXPANSIONS = 8;

/** Settlement poll interval by time since the window ended: 5s, 15s, 60s, then 5 min. */
export function settleRetryMs(sinceEndMs: number): number {
  if (sinceEndMs < 2 * 60_000) return 5_000;
  if (sinceEndMs < 10 * 60_000) return 15_000;
  if (sinceEndMs < 30 * 60_000) return 60_000;
  return 5 * 60_000;
}

interface LongQuote {
  bid: number | null;
  ask: number | null;
  ts: number;
  /** delivered by the stream (which pushes changes): stays valid while the socket is alive */
  ws: boolean;
}

export class MarketTracker extends EventEmitter {
  private readonly log: Logger;
  private readonly cfg: PolyConfig;
  private readonly client: NonNullable<TrackerOptions["client"]>;
  private readonly pollMs: number;
  private readonly quoteMaxAgeMs: number;
  private readonly discoveryMs: number;
  private readonly priceAt: TrackerOptions["priceAt"];
  private readonly unsettled: TrackerOptions["unsettled"];
  private readonly streamOverride: QuoteStream | undefined;
  private readonly now: () => number;

  private running = false;
  private timers: NodeJS.Timeout[] = [];
  private boundaryTimer: NodeJS.Timeout | null = null;
  private stream: QuoteStream | null = null;

  private windows = new Map<string, DiscoveredWindow>();
  private currentId: string | null = null;
  private nextId: string | null = null;
  private quotes = new Map<string, LongQuote>();
  private _lastOdds: OddsSnap | null = null;

  private lastDataTs: number | null = null;
  private lastWsTs: number | null = null; // any WS frame (heartbeats included): liveness
  private lastWsError: string | null = null;
  private lastError: string | null = null;
  private consecutiveErrors = 0;
  private lastStatusKey = "";
  private discovering = false;
  private lastDiscoveryAt = 0;
  private discoveryMisses = 0;
  private nextDiscoveryAt = 0;
  private series: { ids: number[]; until: number } | null = null;
  private polling = false;
  private pollErrors = 0;
  private nextPollAt = 0;
  private restBlockedUntil = 0; // 429 / Retry-After
  private settleTries = new Map<string, number>(); // window id -> last try ts
  private settleSeen = new Map<string, { value: number; firstTs: number }>(); // first read of the current value
  private voided = new Set<string>(); // confirmed non-binary settlement: nothing more to poll
  private settling = new Set<string>();
  private lastWarnAt = 0;

  constructor(opts: TrackerOptions) {
    super();
    this.log = opts.logger;
    this.cfg = { ...loadPolyConfig(), ...(opts.config ?? {}) };
    this.client = opts.client ?? new PolymarketClient({ baseUrl: this.cfg.gatewayUrl, timeoutMs: this.cfg.timeoutMs });
    this.pollMs = Math.max(100, opts.pollMs ?? 1000);
    this.quoteMaxAgeMs = Math.max(STALE_AFTER_MS, 3 * this.pollMs);
    this.discoveryMs = Math.max(1000, opts.discoveryMs ?? 30_000);
    this.priceAt = opts.priceAt;
    this.unsettled = opts.unsettled;
    this.streamOverride = opts.stream;
    this.now = opts.now ?? Date.now;
  }

  // ------------------------------------------------------------------ public API

  start(): void {
    if (this.running) return;
    this.running = true;
    if (this.unsettled) {
      try {
        this.restore(this.unsettled());
      } catch (err) {
        this.log.warn({ err: (err as Error).message }, "polymarket: cannot load unsettled windows");
      }
    }
    void this.discover();
    this.timers.push(setInterval(() => void this.discover(), this.discoveryMs));
    this.timers.push(setInterval(() => void this.poll(), this.pollMs));
    this.timers.push(setInterval(() => this.housekeep(), HOUSEKEEP_MS));
    if (this.streamOverride || this.wsEnabled()) {
      const auth = this.cfg.wsMode === "noauth" ? null : { keyId: this.cfg.keyId!, secretKey: this.cfg.secretKey! };
      const stream = this.streamOverride ?? new MarketsStream({ url: this.cfg.wsUrl, logger: this.log, auth });
      this.stream = stream;
      stream.on("bbo", this.onStreamBbo);
      stream.on("message", this.onStreamMessage);
      stream.on("serverError", this.onStreamError);
      stream.on("open", this.onStreamOpen);
      stream.on("close", this.onStreamClose);
      stream.setSlugs(this.streamSlugs());
      stream.start();
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
    const stream = this.stream;
    this.stream = null;
    if (stream) {
      stream.stop();
      stream.off("bbo", this.onStreamBbo);
      stream.off("message", this.onStreamMessage);
      stream.off("serverError", this.onStreamError);
      stream.off("open", this.onStreamOpen);
      stream.off("close", this.onStreamClose);
    }
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
    // a live socket on a quiet book is still live data: count every WS frame once the stream feeds this window
    const liveTs =
      this.lastDataTs !== null && this.lastWsTs !== null && this.stream?.connected && this.wsBacksCurrent()
        ? Math.max(this.lastDataTs, this.lastWsTs)
        : this.lastDataTs;
    const ageMs = liveTs === null ? null : now - liveTs;
    let state: FeedState;
    let detail: string | undefined;
    const via = this.stream?.connected ? "ws" : "poll";
    const wsNote = this.stream && this.lastWsError ? `ws: ${this.lastWsError}` : null;
    if (!this.running) {
      state = "down";
      detail = "stopped";
    } else if (liveTs === null) {
      state = "down";
      detail = this.lastError ?? wsNote ?? (this.currentId ? "waiting for data" : "no active window");
    } else if (ageMs! > STALE_AFTER_MS) {
      state = this.consecutiveErrors >= 3 ? "down" : "stale";
      detail = this.lastError ?? wsNote ?? (this.currentId ? `no data (${via})` : "no active window");
    } else {
      state = "live";
      detail = wsNote && via === "poll" ? `poll (${wsNote})` : via;
    }
    return { id: "polymarket", state, lastMsgTs: liveTs, ageMs, detail };
  }

  /** All known windows (newest first); handy for the recorder/back-fill. */
  windowsList(): MarketWindow[] {
    return [...this.windows.values()].map((d) => d.window).sort((a, b) => b.startTs - a.startTs);
  }

  /**
   * Restart recovery: re-adopt stored windows that never got a result (ended < SETTLE_GIVE_UP_MS ago, or not
   * ended yet) so the settle loop polls GET /v1/markets/{slug}/settlement for them. Discovery data, when it
   * arrives, merges over these.
   */
  restore(windows: MarketWindow[]): void {
    const now = this.now();
    let n = 0;
    for (const w of windows) {
      if (w.result || this.windows.has(w.id) || now > w.endTs + SETTLE_GIVE_UP_MS) continue;
      const up = parseInstrument(w.upInstrument);
      const down = parseInstrument(w.downInstrument);
      this.windows.set(w.id, {
        window: { ...w, status: statusAt(w, now) },
        upSlug: up.slug,
        upSide: up.side,
        downSlug: down.slug,
        downSide: down.side,
        ptbFromMetadata: w.priceToBeat !== null,
      });
      n++;
    }
    if (n > 0) {
      this.log.info({ windows: n }, "polymarket: re-adopted unsettled windows from storage");
      this.select(now);
    }
  }

  // ------------------------------------------------------------------ discovery

  private wsEnabled(): boolean {
    if (this.cfg.wsMode === "off") return false;
    if (this.cfg.wsMode === "noauth") return true;
    return !!(this.cfg.keyId && this.cfg.secretKey);
  }

  /** Windows found in `events` that cover now .. now + W (the live one or the one right after it). */
  private hasNeeded(events: RawEvent[], now: number, both: boolean): boolean {
    const found = windowsFromEvents(events, now, this.cfg);
    const { current, next } = pickCurrentNext(found, now);
    if (both) return current !== null && next !== null;
    return found.some((d) => d.window.endTs > now && d.window.startTs <= now + this.cfg.windowMs);
  }

  /** GET /v1/series once per SERIES_RESOLVE_MS: ids of the BTC 15m Up/Down series (POLY_SERIES_ID overrides). */
  private async resolveSeries(now: number): Promise<number[]> {
    if (this.cfg.seriesIds.length > 0) return this.cfg.seriesIds;
    const listSeries = this.client.listSeries?.bind(this.client);
    if (!listSeries) return [];
    if (this.series && now < this.series.until) return this.series.ids;
    try {
      const all: Record<string, unknown>[] = [];
      for (let page = 0; page < MAX_LIST_PAGES; page++) {
        const batch = await listSeries({ active: true, limit: LIST_LIMIT, offset: page * LIST_LIMIT });
        all.push(...batch);
        if (batch.length < LIST_LIMIT) break;
      }
      const ids = pickBtc15mSeries(all)
        .map((x) => Number(x.id))
        .filter((n) => Number.isInteger(n) && n > 0);
      this.series = { ids, until: now + SERIES_RESOLVE_MS };
      if (ids.length > 0) this.log.info({ seriesIds: ids }, "polymarket: resolved BTC 15m series");
      return ids;
    } catch (err) {
      this.series = { ids: [], until: now + SERIES_RETRY_MS };
      this.log.debug({ err: (err as Error).message }, "polymarket: series lookup failed; using search");
      return [];
    }
  }

  /** GET /v1/events paged by offset until a short page. */
  private async listPaged(q: Query): Promise<RawEvent[]> {
    const out: RawEvent[] = [];
    for (let page = 0; page < MAX_LIST_PAGES; page++) {
      const batch = await this.client.listEvents({ ...q, limit: LIST_LIMIT, offset: page * LIST_LIMIT });
      out.push(...batch);
      if (batch.length < LIST_LIMIT) break;
    }
    return out;
  }

  /**
   * GET /v1/search paged until a short page, a page with nothing new, or the needed window is found.
   * The page base (0 or 1) is UNVERIFIED: start with the server default, then page=1, 2, … and dedupe.
   */
  private async searchPaged(status: "active" | "upcoming", now: number): Promise<RawEvent[]> {
    const out: RawEvent[] = [];
    const seen = new Set<string>();
    for (let i = 0; i < MAX_SEARCH_PAGES; i++) {
      const batch = await this.client.searchEvents({
        query: this.cfg.searchQuery,
        status,
        limit: SEARCH_LIMIT,
        page: i === 0 ? undefined : i,
      });
      let fresh = 0;
      for (const e of batch) {
        const key = String(e.slug ?? e.id ?? JSON.stringify(e));
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(e);
        fresh++;
      }
      if (batch.length < SEARCH_LIMIT || (fresh === 0 && i >= 2) || this.hasNeeded(out, now, false)) break;
    }
    return out;
  }

  /**
   * Candidate events: the BTC 15m series (POLY_SERIES_ID or resolved via GET /v1/series) and POLY_TAG_SLUG
   * listings first; the text search runs only when those did not yield both the live and the next window.
   * Tolerates partial failure.
   */
  private async fetchEvents(now: number): Promise<RawEvent[]> {
    const c = this.client;
    const events: RawEvent[] = [];
    const errors: string[] = [];
    let firstErr: unknown = null;
    const collect = async (job: () => Promise<RawEvent[]>) => {
      try {
        events.push(...(await job()));
      } catch (err) {
        firstErr ??= err;
        errors.push((err as Error)?.message ?? String(err));
      }
    };
    // Direct: the event slugs are predictable from the window start (seen live 2026-10-21). This is
    // the reliable path; the text search below returns dozens of BTC 15m events (they are created
    // ~12h ahead) in an unknown order, and can miss the live or next window.
    if (this.cfg.eventSlugTemplate) {
      const direct = await Promise.allSettled(
        eventSlugsAround(this.cfg.eventSlugTemplate, now, this.cfg.windowMs).map((sl) => c.getEventBySlug(sl)),
      );
      for (const r of direct) {
        if (r.status === "fulfilled" && r.value) events.push(r.value);
        else if (r.status === "rejected") {
          firstErr ??= r.reason;
          errors.push((r.reason as Error)?.message ?? String(r.reason));
        }
      }
      if (this.hasNeeded(events, now, true)) return events;
    }
    const seriesIds = await this.resolveSeries(now);
    const listings: Promise<void>[] = [];
    if (seriesIds.length > 0) listings.push(collect(() => this.listPaged({ seriesId: seriesIds, active: true, closed: false })));
    if (this.cfg.tagSlug) listings.push(collect(() => this.listPaged({ tagSlug: this.cfg.tagSlug, active: true, closed: false })));
    await Promise.all(listings);
    if (!this.hasNeeded(events, now, true)) {
      await Promise.all([collect(() => this.searchPaged("active", now)), collect(() => this.searchPaged("upcoming", now))]);
    }
    if (events.length === 0 && errors.length > 0) throw firstErr instanceof Error ? firstErr : new Error(errors[0]);

    // events listed without nested markets: fetch them by slug (bounded, nearest to now first)
    const bySlug = new Map<string, RawEvent>();
    for (const e of events) if (typeof e.slug === "string") bySlug.set(e.slug, e);
    const distance = (e: RawEvent) => {
      const t = extractTimes(e, e, this.cfg, now);
      return t ? Math.abs(t.startTs - now) : Infinity;
    };
    const missing = [...bySlug.values()]
      .filter((e) => !Array.isArray(e.markets) || e.markets.length === 0)
      .filter((e) => isBtc15mEvent({ ...e, markets: [] }, this.cfg, now))
      .sort((a, b) => distance(a) - distance(b))
      .slice(0, MAX_EVENT_EXPANSIONS);
    await Promise.allSettled(
      missing.map(async (e) => {
        const full = await c.getEventBySlug(String(e.slug));
        if (full) bySlug.set(String(e.slug), full);
      }),
    );
    return [...bySlug.values()];
  }

  async discover(): Promise<void> {
    if (!this.running || this.discovering || this.now() < this.restBlockedUntil) return;
    this.discovering = true;
    const now = this.now();
    this.lastDiscoveryAt = now;
    try {
      const events = await this.fetchEvents(now);
      const found = windowsFromEvents(events, this.now(), this.cfg);
      this.ingest(found);
      if (found.length === 0) {
        this.discoveryMissed();
        this.warn("polymarket discovery: no BTC 15m windows found", { events: events.length });
      } else {
        this.discoveryMisses = 0;
        this.nextDiscoveryAt = now + DISCOVERY_RETRY_MS;
      }
    } catch (err) {
      this.discoveryMissed();
      this.noteError(`discovery: ${(err as Error).message}`, err);
    } finally {
      this.discovering = false;
    }
  }

  /** Empty or failed discovery: the eager re-discovery backs off 5s -> 60s (with jitter). */
  private discoveryMissed(): void {
    this.nextDiscoveryAt = this.now() + backoffMs(this.discoveryMisses++, DISCOVERY_RETRY_MS, DISCOVERY_BACKOFF_MAX_MS);
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
    if (curChanged && newCur) {
      // the new window was "next": with the WS its quotes are usually cached already -> publish them now,
      // and poll right away instead of waiting for the old window's data to age.
      this.emitOdds();
      this.nextPollAt = 0;
      if (this.running) void this.poll();
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

    // discover eagerly near/after a boundary when the next window is unknown (backs off while it keeps missing)
    const cur = this.current();
    const needSoon = !cur || (!this.nextId && cur.endTs - now < 120_000);
    if (needSoon && now - this.lastDiscoveryAt > DISCOVERY_RETRY_MS && now >= this.nextDiscoveryAt) void this.discover();

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
      const p = this.proxyMinuteAvg(w.startTs);
      if (p !== null) {
        d.window = { ...w, priceToBeat: p };
        this.log.info({ id: w.id, ptb: p }, "polymarket: PTB from BRTI proxy (metadata lacked it)");
        this.emit("window", d.window);
      }
    }
  }

  private proxyAt(ts: number): number | null {
    if (!this.priceAt) return null;
    let p: number | null = null;
    try {
      p = this.priceAt(ts);
    } catch {
      p = null;
    }
    return p !== null && Number.isFinite(p) && p > 0 ? p : null;
  }

  /**
   * The settlement rule from the market description: "in the last minute before each of those two times,
   * 60 BRTI prices are collected; the official value is their simple average". Mirrors it on the proxy
   * (one sample per second over [ts-60s, ts)); falls back to the price at ts with too few samples.
   */
  private proxyMinuteAvg(ts: number): number | null {
    let sum = 0;
    let n = 0;
    for (let i = 60; i >= 1; i--) {
      const p = this.proxyAt(ts - i * 1000);
      if (p !== null) {
        sum += p;
        n++;
      }
    }
    if (n >= 30) return Math.round((sum / n) * 100) / 100;
    return this.proxyAt(ts);
  }

  /** What the BRTI proxy at the window end says, or null when it is unknown or too close to the PTB to call. */
  private proxyResult(w: MarketWindow): "up" | "down" | null {
    const p = this.proxyMinuteAvg(w.endTs);
    const ptb = w.priceToBeat;
    if (p === null || ptb === null || ptb <= 0) return null;
    if (Math.abs(p - ptb) / ptb < SETTLE_PROXY_MARGIN) return null;
    return p >= ptb ? "up" : "down";
  }

  private settle(now: number): void {
    if (now < this.restBlockedUntil) return;
    for (const d of this.windows.values()) {
      const w = d.window;
      if (w.result || this.voided.has(w.id)) continue;
      if (now < w.endTs + SETTLE_FIRST_DELAY_MS || now > w.endTs + SETTLE_GIVE_UP_MS) continue;
      if (this.settling.has(w.id)) continue;
      const last = this.settleTries.get(w.id) ?? 0;
      if (now - last < settleRetryMs(now - w.endTs)) continue;
      this.settleTries.set(w.id, now);
      this.settling.add(w.id);
      void this.trySettle(d).finally(() => this.settling.delete(w.id));
    }
  }

  /**
   * A result cannot be undone (the recorder keeps the first one), so a settlement value is accepted only when:
   *  - two reads at least SETTLE_CONFIRM_MS apart return the same value (an unsettled market may answer 0);
   *  - /book (when available) is readable and not OPEN/PREOPEN; a getBook error means "unknown, retry";
   *  - the book state is terminal (EXPIRED/TERMINATED/CLOSED) and the BRTI proxy at the window end does not
   *    clearly contradict it; otherwise the value is held until it has been stable for SETTLE_HOLD_MAX_MS.
   * A confirmed non-binary value (0.5 = void) gives no Up/Down result and stops the polling.
   */
  private async trySettle(d: DiscoveredWindow): Promise<void> {
    const id = d.window.id;
    try {
      const s = await this.client.getSettlement(d.upSlug);
      const now = this.now();
      if (!s) {
        this.settleSeen.delete(id);
        return;
      }
      const seen = this.settleSeen.get(id);
      if (!seen || seen.value !== s.value) {
        this.settleSeen.set(id, { value: s.value, firstTs: now });
        return;
      }
      const stableMs = now - seen.firstTs;
      if (stableMs < SETTLE_CONFIRM_MS) return;

      let state: string | null = null;
      if (this.client.getBook) {
        const book = await this.client.getBook(d.upSlug); // throws -> unknown, retry later
        state = book?.state ?? null;
        if (state && OPEN_STATES.has(state)) {
          this.settleSeen.delete(id);
          return;
        }
      }
      const cur = this.windows.get(id);
      if (!cur || cur.window.result) return;
      const result = settlementToResult(s.value, d.upSide === "long");
      if (result === null) {
        this.voided.add(id);
        this.log.warn({ id, value: s.value }, "polymarket: non-binary settlement (void?); window left without a result");
        return;
      }
      const proxy = this.proxyResult(cur.window);
      const doubt =
        proxy !== null && proxy !== result
          ? `BRTI proxy says ${proxy}`
          : state !== null && !TERMINAL_STATES.has(state)
            ? `book state ${state}`
            : null;
      if (doubt && stableMs < SETTLE_HOLD_MAX_MS) {
        this.warn("polymarket: holding settlement", { id, value: s.value, result, doubt });
        return;
      }
      cur.window = { ...cur.window, result, status: "settled" };
      this.settleTries.delete(id);
      this.settleSeen.delete(id);
      this.log.info({ id, result, value: s.value, doubt }, "polymarket: window settled");
      this.emit("window", cur.window);
      this.emit("settled", cur.window);
    } catch (err) {
      this.noteRateLimit(err);
      this.log.debug({ id, err: (err as Error).message }, "polymarket: settlement not available yet");
    }
  }

  private prune(now: number): void {
    for (const [id, d] of this.windows) {
      if (id === this.currentId || id === this.nextId) continue;
      const w = d.window;
      const done = w.result || this.voided.has(id) ? w.endTs + KEEP_FINISHED_MS : w.endTs + SETTLE_GIVE_UP_MS;
      if (now > done) {
        this.windows.delete(id);
        this.settleTries.delete(id);
        this.settleSeen.delete(id);
        this.voided.delete(id);
      }
    }
  }

  // ------------------------------------------------------------------ quotes

  /** Fresh = updated within `bound`, or delivered by a stream that is still alive (it pushes every change). */
  private quoteFresh(q: LongQuote | undefined, now: number, bound: number): q is LongQuote {
    if (!q) return false;
    if (now - q.ts <= bound) return true;
    return q.ws && !!this.stream?.connected && this.lastWsTs !== null && now - this.lastWsTs <= bound;
  }

  private wsBacksCurrent(): boolean {
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    return !!d && (!!this.quotes.get(d.upSlug)?.ws || !!this.quotes.get(d.downSlug)?.ws);
  }

  private async poll(): Promise<void> {
    if (!this.running || this.polling) return;
    const now = this.now();
    if (now < this.nextPollAt || now < this.restBlockedUntil) return;
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    if (!d) return;
    const slugs = [...new Set([d.upSlug, d.downSlug])];
    // the WS is delivering (or alive on a quiet book) for every side: skip the REST poll
    if (this.stream?.connected && slugs.every((sl) => this.quoteFresh(this.quotes.get(sl), now, 1.5 * this.pollMs))) return;
    this.polling = true;
    let rolled = false;
    try {
      const bbos = await Promise.all(slugs.map((s) => this.client.getBbo(s)));
      if (this.currentId !== d.window.id) {
        rolled = true; // rolled over mid-request: these quotes belong to the old window
        return;
      }
      let any = false;
      for (let i = 0; i < slugs.length; i++) {
        const b = bbos[i];
        if (b) {
          this.storeQuote(slugs[i]!, b, false);
          any = true;
        } else {
          this.quotes.delete(slugs[i]!); // unknown market: never keep publishing its old quote
        }
      }
      if (!any) throw new Error("bbo: empty response");
      this.pollErrors = 0;
      this.consecutiveErrors = 0;
      this.lastError = null;
      this.lastDataTs = this.now();
      this.emitOdds();
    } catch (err) {
      if (this.currentId !== d.window.id) {
        rolled = true;
        return;
      }
      this.pollErrors++;
      if (this.pollErrors >= POLL_BACKOFF_AFTER) {
        this.nextPollAt = this.now() + backoffMs(this.pollErrors - POLL_BACKOFF_AFTER, 2 * this.pollMs, POLL_BACKOFF_MAX_MS);
      }
      this.noteError(`bbo: ${(err as Error).message}`, err);
    } finally {
      this.polling = false;
      if (rolled && this.running) void this.poll();
    }
  }

  private storeQuote(slug: string, b: NormalizedBbo, ws: boolean): void {
    this.quotes.set(slug, { bid: b.bid, ask: b.ask, ts: this.now(), ws });
  }

  private readonly onStreamBbo = (b: NormalizedBbo): void => {
    const slug = b.slug;
    if (!slug) return;
    this.storeQuote(slug, b, true);
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    if (d && (slug === d.upSlug || slug === d.downSlug)) {
      this.lastDataTs = this.now();
      this.lastWsError = null;
      this.consecutiveErrors = 0;
      this.lastError = null;
      this.emitOdds();
    }
  };

  private readonly onStreamMessage = (): void => {
    this.lastWsTs = this.now();
  };

  private readonly onStreamError = (err: unknown): void => {
    this.lastWsError = String(err);
    this.emitStatus(false);
  };

  private readonly onStreamOpen = (): void => {
    this.lastWsTs = this.now();
    // anything cached before the reconnect may have missed changes: it must be refreshed to count as fresh
    for (const q of this.quotes.values()) q.ws = false;
    this.emitStatus(false);
  };

  private readonly onStreamClose = (): void => {
    this.emitStatus(false);
  };

  /** Build the OddsSnap for the current window from the stored long-side quotes; aged-out sides are null. */
  private emitOdds(): void {
    const d = this.currentId ? this.windows.get(this.currentId) : undefined;
    if (!d) return;
    const now = this.now();
    const upQ = this.quotes.get(d.upSlug);
    const downQ = this.quotes.get(d.downSlug);
    const upOk = this.quoteFresh(upQ, now, this.quoteMaxAgeMs);
    const downOk = this.quoteFresh(downQ, now, this.quoteMaxAgeMs);
    if (!upOk && !downOk) return; // nothing known for this window (yet)
    const up = upOk ? sidePrices(upQ, d.upSide) : { bid: null, ask: null };
    const down = downOk ? sidePrices(downQ, d.downSide) : { bid: null, ask: null };
    const snap = buildOddsSnap(now, d.window.id, up, down);
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

  /** 429 (or any Retry-After): pause every REST loop for the requested time. */
  private noteRateLimit(err: unknown): void {
    if (!(err instanceof PolyHttpError)) return;
    if (err.status !== 429 && err.retryAfterMs === null) return;
    const wait = Math.min(RATE_LIMIT_MAX_MS, err.retryAfterMs ?? RATE_LIMIT_DEFAULT_MS);
    this.restBlockedUntil = Math.max(this.restBlockedUntil, this.now() + wait);
  }

  private noteError(msg: string, err?: unknown): void {
    this.noteRateLimit(err);
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
