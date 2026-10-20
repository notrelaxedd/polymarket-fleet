import { EventEmitter } from "node:events";
import type {
  Bar,
  BookLevels,
  BtcTick,
  FairSnap,
  FeedStatus,
  MarketWindow,
  OddsSnap,
  RiskLimits,
  ServerMsg,
  Settings,
  StateSnapshot,
} from "../../shared/protocol.ts";
import { STALE_AFTER_MS } from "../../shared/protocol.ts";
import type { Config } from "./config.ts";
import type { TerminalDb } from "./db/index.ts";
import { loadRiskLimits, loadSettings, saveSettings, SettingsPatchSchema, type StoredSettings } from "./db/settings.ts";
import { edgeCents, fairCents, RealizedVol } from "./fair/index.ts";
import { BarAggregator, BtcProxy, fetchRecentBars } from "./feeds/index.ts";
import type { Logger } from "./log.ts";
import {
  FEE_SCHEDULE,
  MarketTracker,
  parseInstrument,
  PolymarketClient,
  settlementToResult,
  type NormalizedBook,
  type NormalizedSettlement,
} from "./polymarket/index.ts";
import type { Recorder } from "./recorder.ts";

/** Up book poll interval (GET /v1/markets/{slug}/book) and its backoff cap after consecutive failures. */
export const BOOK_POLL_MS = 1000;
const BOOK_BACKOFF_MAX_MS = 15_000;
/** Levels kept per side of the Up book. */
const BOOK_DEPTH = 10;

/**
 * Typed events the Engine emits besides "msg" (for backend modules: trading watcher, alerts, risk...).
 *   tick     every BRTI-proxy tick            rollover  at each window boundary
 *   odds     every top-of-book update         settled   a window got its result
 *   fair     every fair recompute (<= 4/s)    settings  settings changed
 *   book     Up book depth (~1/s)             window    any window insert/update
 */
export interface EngineEvents {
  tick: BtcTick;
  odds: OddsSnap;
  fair: FairSnap;
  book: BookLevels;
  window: MarketWindow;
  rollover: { current: MarketWindow | null; next: MarketWindow | null };
  settled: MarketWindow;
  settings: Settings;
  msg: ServerMsg;
}

/** Up book source (PolymarketClient implements it). */
export interface BookSource {
  getBook(slug: string): Promise<NormalizedBook | null>;
}

/** Min gap between fair recomputes. */
export const FAIR_MIN_GAP_MS = 250;
/** Persisted fair / odds rows: at most one per RECORD_GAP_MS bucket (per window). */
export const RECORD_GAP_MS = 1000;
/** In-memory ring of recent 1s bars (for PTB fallback lookups). */
const RING_MAX = 2 * 3600;
/** A BTC tick or Polymarket quote older than this is too stale to compute fair / edge from (= feed "stale"). */
const MAX_INPUT_AGE_MS = STALE_AFTER_MS;
/** How many finished windows the snapshot carries. */
const RECENT_WINDOWS = 20;
/** Hours of 60s candles fetched at start to cover a gap while the worker was down (PTB fallback, vol). */
const BACKFILL_HOURS = 2;
/** Settlement back-fill: windows that ended while the worker was down (same horizon as the tracker). */
const SETTLE_DELAY_MS = 5_000;
const SETTLE_GIVE_UP_MS = 6 * 3600_000;
const SETTLE_RETRY_MS = 30_000;
const OPEN_BOOK_STATES = new Set(["MARKET_STATE_OPEN", "MARKET_STATE_PREOPEN", "OPEN", "PREOPEN"]);

/**
 * Realized vol must cover at least this many seconds of history before fair value is published:
 * half the lookback, capped at 5 minutes (so ~30 noisy 1s returns on a cold start don't count).
 */
export function minVolSpanSec(lookbackMin: number): number {
  return Math.min(lookbackMin * 60 * 0.5, 300);
}

/** Min returns for sigma; the covered-time gate (minVolSpanSec) is the real warm-up requirement. */
const VOL_MIN_SAMPLES = 10;
const newVol = (lookbackMin: number) => new RealizedVol(lookbackMin * 60, 1, VOL_MIN_SAMPLES);

const sameBucket = (a: number, b: number): boolean =>
  Math.floor(a / RECORD_GAP_MS) === Math.floor(b / RECORD_GAP_MS);

/** Minimal surface of BtcProxy the engine relies on (lets tests inject fakes). */
export interface BtcSource extends EventEmitter {
  start(): void;
  stop(): void;
  status(): FeedStatus[];
  last(): BtcTick | null;
}

/** Minimal surface of MarketTracker the engine relies on. */
export interface WindowSource extends EventEmitter {
  start(): void;
  stop(): void;
  current(): MarketWindow | null;
  next(): MarketWindow | null;
  lastOdds(): OddsSnap | null;
  status(): FeedStatus;
}

/** Settlement lookups (PolymarketClient implements it). */
export interface SettlementSource {
  getSettlement(slug: string): Promise<NormalizedSettlement | null>;
  getBook?(slug: string): Promise<NormalizedBook | null>;
}

export class SettingsError extends Error {
  readonly statusCode = 400;
}

export interface EngineDeps {
  config: Config;
  db: TerminalDb;
  recorder: Recorder;
  logger: Logger;
  /** inject for tests; default = real BtcProxy */
  btc?: BtcSource;
  /** inject for tests; default = real MarketTracker */
  tracker?: WindowSource;
  /** recent 60s candles for the start-up gap back-fill; default = Coinbase (fetchRecentBars), null = off */
  fetchBars?: ((hours: number) => Promise<Bar[]>) | null;
  /** settlement back-fill for windows that ended while down; default = PolymarketClient, null = off */
  settlements?: SettlementSource | null;
  /** Up book depth polling; default = PolymarketClient when the engine builds its own tracker, null = off */
  books?: BookSource | null;
  feeNote?: string;
}

/**
 * Wires the BRTI proxy, 1s bar aggregation, realized vol, the Polymarket market tracker
 * and the recorder together. Emits "msg" (ServerMsg) for the WebSocket layer to broadcast.
 */
export class Engine extends EventEmitter {
  private readonly cfg: Config;
  private readonly db: TerminalDb;
  private readonly recorder: Recorder;
  private readonly log: Logger;
  private readonly btc: BtcSource;
  private readonly tracker: WindowSource;
  private readonly agg = new BarAggregator(1);
  private vol: RealizedVol;
  private settings: StoredSettings;
  private riskLimits: RiskLimits;
  private readonly books: BookSource | null;
  private lastBook: BookLevels | null = null;
  private bookTimer: NodeJS.Timeout | null = null;
  private bookBusy = false;
  private bookErrors = 0;
  private nextBookAt = 0;
  private readonly snapshotParts: (() => Partial<StateSnapshot>)[] = [];
  private readonly feeNote: string;
  private readonly fetchBars: ((hours: number) => Promise<Bar[]>) | null;
  private readonly settlements: SettlementSource | null;

  private ring: Bar[] = [];
  private lastTick: BtcTick | null = null;
  private lastOdds: OddsSnap | null = null;
  private lastFair: FairSnap | null = null;
  private lastFairAt = 0;
  private lastFairRecord: { windowId: string; ts: number } | null = null;
  private lastOddsRecord: { windowId: string; ts: number } | null = null;
  private fairTimer: NodeJS.Timeout | null = null;
  private clock: NodeJS.Timeout | null = null;
  private settleTimer: NodeJS.Timeout | null = null;
  private settleBusy = false;
  /** ended windows without a result that the tracker may not know about (from the DB at start) */
  private readonly pendingSettle = new Map<string, MarketWindow>();
  private running = false;
  /** the engine built its own MarketTracker and handed it the stored unsettled windows (tracker.restore) */
  private readonly trackerRestores: boolean;

  constructor(deps: EngineDeps) {
    super();
    this.setMaxListeners(0);
    this.cfg = deps.config;
    this.db = deps.db;
    this.recorder = deps.recorder;
    this.log = deps.logger.child({ component: "engine" });
    this.feeNote = deps.feeNote ?? FEE_SCHEDULE.note;
    this.settings = loadSettings(this.db, {
      mode: this.cfg.tradingMode,
      volLookbackMin: this.cfg.volLookbackMin,
      feeCentsPerContract: this.cfg.feeCentsPerContract,
    });
    this.riskLimits = loadRiskLimits(this.db);
    if (this.settings.mode === "live" && !this.hasTradingKeys()) {
      this.log.warn("stored mode is live but POLYMARKET_US_KEY_ID/SECRET are missing: starting in paper");
      this.settings = { ...this.settings, mode: "paper" };
    }
    this.fetchBars = deps.fetchBars !== undefined ? deps.fetchBars : fetchRecentBars;
    this.settlements =
      deps.settlements !== undefined ? deps.settlements : new PolymarketClient({ baseUrl: this.cfg.polyGatewayUrl });
    this.books =
      deps.books !== undefined ? deps.books : !deps.tracker && this.settlements?.getBook ? (this.settlements as BookSource) : null;
    this.vol = newVol(this.settings.volLookbackMin);
    this.btc = deps.btc ?? new BtcProxy({ logger: deps.logger });
    this.trackerRestores = !deps.tracker;
    this.tracker =
      deps.tracker ??
      new MarketTracker({
        logger: deps.logger,
        pollMs: this.cfg.polyPollMs,
        priceAt: (ts: number) => this.priceAt(ts),
        config: { gatewayUrl: this.cfg.polyGatewayUrl },
        // restart recovery: the tracker re-adopts stored windows without a result and settles them with its
        // full confirmation logic (two agreeing reads, closed book, BRTI cross-check)
        unsettled: () => this.db.listWindows(200),
      });
  }

  // ------------------------------------------------------------------ lifecycle

  start(): void {
    if (this.running) return;
    this.running = true;
    this.warmVol();
    this.recorder.start();

    this.btc.on("tick", this.onTick);
    this.btc.on("error", this.onSourceError);
    this.tracker.on("odds", this.onOdds);
    this.tracker.on("window", this.onWindow);
    this.tracker.on("settled", this.onSettled);
    this.tracker.on("rollover", this.onRollover);
    this.tracker.on("error", this.onSourceError);

    this.btc.start();
    this.tracker.start();
    // fair depends on time-left too, so refresh it even when BTC is quiet
    this.clock = setInterval(() => this.requestFair(), 1000);
    if (this.books) this.bookTimer = setInterval(() => void this.pollBook(), BOOK_POLL_MS);
    void this.backfillBars();
    this.startSettleBackfill();
    this.log.info(
      { settings: this.settings, sigma: this.volSigma(), volSamples: this.vol.count(), volSpanSec: this.vol.spanSec() },
      "engine started",
    );
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    if (this.clock) clearInterval(this.clock);
    if (this.fairTimer) clearTimeout(this.fairTimer);
    if (this.settleTimer) clearInterval(this.settleTimer);
    if (this.bookTimer) clearInterval(this.bookTimer);
    this.clock = this.fairTimer = this.settleTimer = this.bookTimer = null;
    try {
      this.btc.stop();
    } catch (err) {
      this.log.warn({ err }, "btc proxy stop failed");
    }
    try {
      this.tracker.stop();
    } catch (err) {
      this.log.warn({ err }, "tracker stop failed");
    }
    this.btc.off("tick", this.onTick);
    this.btc.off("error", this.onSourceError);
    this.tracker.off("odds", this.onOdds);
    this.tracker.off("window", this.onWindow);
    this.tracker.off("settled", this.onSettled);
    this.tracker.off("rollover", this.onRollover);
    this.tracker.off("error", this.onSourceError);
    // persist the in-progress bar so a restart doesn't leave a hole
    const open = this.agg.current();
    if (open) this.recorder.addBar(open);
    this.recorder.stop();
    this.log.info("engine stopped");
  }

  // ------------------------------------------------------------------ public API

  snapshot(): StateSnapshot {
    const current = this.tracker.current();
    const odds = this.lastOdds ?? this.tracker.lastOdds();
    const extra: Partial<StateSnapshot> = {};
    for (const part of this.snapshotParts) {
      try {
        Object.assign(extra, part());
      } catch (err) {
        this.log.warn({ err }, "snapshot extension failed");
      }
    }
    return {
      ...extra,
      book: this.currentBook(),
      serverTime: Date.now(),
      settings: this.getSettings(),
      current,
      next: this.tracker.next(),
      recent: this.recentWindows(),
      lastTick: this.lastTick ?? this.btc.last(),
      // top of book / fair for the CURRENT window only (never the previous window's after a rollover)
      lastOdds: odds && odds.windowId === current?.id ? odds : null,
      lastFair: this.lastFair && this.lastFair.windowId === current?.id ? this.lastFair : null,
      feeds: this.feeds(),
    };
  }

  feeds(): FeedStatus[] {
    return [...this.btc.status(), this.tracker.status()];
  }

  getSettings(): Settings {
    return { ...this.settings, feeNote: this.feeNote, risk: { ...this.riskLimits }, liveAvailable: this.hasTradingKeys() };
  }

  // ---- state accessors for backend modules (see modules/types.ts EngineApi) ----

  /** Add fields to every snapshot (orders, positions, risk, alerts, drawings...). Later parts win. */
  private readonly settingsGuards: ((next: { mode: Settings["mode"] }, prev: Settings) => string | null)[] = [];

  /**
   * Register a check run before a settings change is applied; returning a message refuses the change (400).
   * Trading uses it to refuse leaving live mode while live positions could still need the exchange.
   */
  addSettingsGuard(fn: (next: { mode: Settings["mode"] }, prev: Settings) => string | null): void {
    this.settingsGuards.push(fn);
  }

  extendSnapshot(part: () => Partial<StateSnapshot>): void {
    this.snapshotParts.push(part);
  }

  /** Emit a ServerMsg to every WebSocket client. */
  publish(msg: ServerMsg): void {
    this.broadcast(msg);
  }

  currentWindow(): MarketWindow | null {
    return this.tracker.current();
  }

  nextWindow(): MarketWindow | null {
    return this.tracker.next();
  }

  latestTick(): BtcTick | null {
    return this.lastTick ?? this.btc.last();
  }

  /** Top of book of the CURRENT window only, and only while fresh (<= STALE_AFTER_MS old); else null. */
  latestOdds(now = Date.now()): OddsSnap | null {
    const o = this.lastOdds ?? this.tracker.lastOdds();
    const cur = this.tracker.current();
    return o && cur && o.windowId === cur.id && now - o.ts <= MAX_INPUT_AGE_MS ? o : null;
  }

  /** Fair of the CURRENT window only (null when stale or another window's). */
  latestFair(now = Date.now()): FairSnap | null {
    const f = this.lastFair;
    const cur = this.tracker.current();
    return f && cur && f.windowId === cur.id && now - f.ts <= MAX_INPUT_AGE_MS ? f : null;
  }

  /** Last Up book of the current window (may be up to a few seconds old; check `ts`). */
  currentBook(): BookLevels | null {
    const b = this.lastBook;
    return b && b.windowId === this.tracker.current()?.id ? b : null;
  }

  /** Fetch the current window's Up book now (paper fills want a fresh one); falls back to the cached book. */
  async refreshBook(): Promise<BookLevels | null> {
    if (!this.books) return this.currentBook();
    try {
      const b = await this.fetchBook();
      if (b) this.setBook(b);
    } catch (err) {
      this.log.debug({ err: (err as Error).message }, "book refresh failed");
    }
    return this.currentBook();
  }

  liveAvailable(): boolean {
    return this.hasTradingKeys();
  }

  /** Apply a partial settings update. Throws SettingsError (400) on invalid input. */
  updateSettings(patch: unknown): Settings {
    const parsed = SettingsPatchSchema.safeParse(patch ?? {});
    if (!parsed.success) {
      throw new SettingsError(parsed.error.issues.map((i) => `${i.path.join(".") || "body"}: ${i.message}`).join("; "));
    }
    const p = parsed.data;
    if (p.risk !== undefined) {
      this.riskLimits = { ...this.riskLimits, ...p.risk };
      saveSettings(this.db, { risk: this.riskLimits });
    }
    if (p.mode === "live" && this.settings.mode !== "live") {
      if (!this.hasTradingKeys()) {
        throw new SettingsError("live mode requires POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET in the backend .env");
      }
    }
    if (p.mode !== undefined && p.mode !== this.settings.mode) {
      const prev = this.getSettings();
      for (const g of this.settingsGuards) {
        const why = g({ mode: p.mode }, prev);
        if (why) throw new SettingsError(why);
      }
    }
    const next: StoredSettings = { ...this.settings };
    // persist only what the user explicitly set; untouched keys keep following the .env defaults
    const changed: Partial<StoredSettings> = {};
    if (p.mode !== undefined) next.mode = changed.mode = p.mode;
    if (p.feeCentsPerContract !== undefined) next.feeCentsPerContract = changed.feeCentsPerContract = p.feeCentsPerContract;
    if (p.volLookbackMin !== undefined) {
      next.volLookbackMin = changed.volLookbackMin = p.volLookbackMin;
      if (p.volLookbackMin !== this.settings.volLookbackMin) this.applyLookback(p.volLookbackMin, this.settings.volLookbackMin);
    }
    saveSettings(this.db, changed);
    this.settings = next;
    const s = this.getSettings();
    this.log.info({ settings: s }, "settings updated");
    this.broadcast({ type: "settings", data: s });
    this.emit("settings", s);
    this.requestFair();
    return s;
  }

  /**
   * BTC proxy price at ts for the PTB fallback: close of the recorded 1s bar whose
   * close time (t + 1s) is nearest ts, from the in-memory ring, else the DB; when no 1s
   * bar closes within 1s of ts, a 60s bar (seeded / start-up back-fill) closing nearer
   * ts is used too (exact for minute-aligned window starts).
   */
  priceAt(ts: number, tolMs = 10_000): number | null {
    if (!Number.isFinite(ts)) return null;
    let best: Bar | null = null;
    let bestDist = Infinity;
    const consider = (b: Bar | null | undefined) => {
      if (!b) return;
      const dist = Math.abs(b.t + b.d * 1000 - ts);
      if (dist < bestDist) {
        best = b;
        bestDist = dist;
      }
    };
    const ring = this.ring;
    if (ring.length) {
      // first bar whose close time >= ts
      let lo = 0;
      let hi = ring.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (ring[mid]!.t + 1000 < ts) lo = mid + 1;
        else hi = mid;
      }
      consider(ring[lo - 1]);
      consider(ring[lo]);
    }
    consider(this.agg.current());
    try {
      if (bestDist > tolMs) consider(this.db.nearestBar1s(ts, tolMs));
      if (bestDist > 1000) consider(this.db.nearestBar(ts, 60, tolMs));
    } catch (err) {
      this.log.warn({ err }, "priceAt db lookup failed");
    }
    return bestDist <= tolMs ? (best as Bar | null)?.c ?? null : null;
  }

  // ------------------------------------------------------------------ handlers

  private readonly onTick = (tick: BtcTick): void => {
    if (!tick || !Number.isFinite(tick.price) || tick.price <= 0) return;
    this.lastTick = tick;
    this.recorder.addTick(tick);
    const closed = this.agg.push(tick.ts, tick.price);
    if (closed) {
      this.recorder.addBar(closed);
      this.ring.push(closed);
      if (this.ring.length > RING_MAX * 1.25) this.ring = this.ring.slice(-RING_MAX);
    }
    this.vol.push(tick.ts, tick.price);
    this.broadcast({ type: "btc", data: tick });
    this.emit("tick", tick);
    this.requestFair();
  };

  private readonly onOdds = (odds: OddsSnap): void => {
    if (!odds) return;
    this.lastOdds = odds;
    const rec = this.lastOddsRecord;
    // one row per 1s bucket: a fixed ">= 1000 ms since last" gate drops ~half of 1 Hz polls (jitter)
    if (!rec || rec.windowId !== odds.windowId || !sameBucket(odds.ts, rec.ts)) {
      this.recorder.addOdds(odds);
      this.lastOddsRecord = { windowId: odds.windowId, ts: odds.ts };
    }
    this.broadcast({ type: "odds", data: odds });
    this.emit("odds", odds);
    this.requestFair();
  };

  private readonly onWindow = (w: MarketWindow): void => {
    if (!w) return;
    if (w.result) this.pendingSettle.delete(w.id);
    this.recorder.addWindow(w);
    this.broadcast({ type: "window", data: w });
    this.emit("window", w);
    if (w.result) this.emit("settled", w);
    if (w.id === this.tracker.current()?.id) this.requestFair();
  };

  /** The tracker also emits "window" for a settlement; just make sure it is persisted and log it. */
  private readonly onSettled = (w: MarketWindow): void => {
    if (!w) return;
    this.pendingSettle.delete(w.id);
    this.recorder.addWindow(w);
    this.log.info({ id: w.id, result: w.result, settlePrice: w.settlePrice, ptb: w.priceToBeat }, "window settled");
    // (the tracker's "window" event for the same settlement already emitted "settled")
  };

  private readonly onRollover = (r: { current: MarketWindow | null; next: MarketWindow | null }): void => {
    if (r.current) this.recorder.addWindow(r.current);
    if (r.next) this.recorder.addWindow(r.next);
    if (this.lastFair && this.lastFair.windowId !== r.current?.id) this.lastFair = null;
    if (this.lastOdds && this.lastOdds.windowId !== r.current?.id) this.lastOdds = null;
    if (this.lastBook && this.lastBook.windowId !== r.current?.id) this.lastBook = null;
    this.log.info({ current: r.current?.id ?? null, next: r.next?.id ?? null }, "rollover");
    this.broadcast({ type: "rollover", data: { current: r.current, next: r.next } });
    this.emit("rollover", { current: r.current, next: r.next });
    this.requestFair();
    if (this.books) void this.pollBook(true);
  };

  private readonly onSourceError = (err: unknown): void => {
    this.log.warn({ err }, "feed error");
  };

  // ------------------------------------------------------------------ fair value

  private requestFair(): void {
    if (!this.running) return;
    const now = Date.now();
    const wait = this.lastFairAt + FAIR_MIN_GAP_MS - now;
    if (wait <= 0) {
      this.computeFair(now);
    } else if (!this.fairTimer) {
      this.fairTimer = setTimeout(() => {
        this.fairTimer = null;
        if (this.running) this.computeFair(Date.now());
      }, wait);
    }
  }

  private computeFair(now: number): void {
    this.lastFairAt = now;
    const win = this.tracker.current();
    const K = win?.priceToBeat;
    if (!win || K == null || !(K > 0)) return;
    if (now < win.startTs || now > win.endTs) return;
    // BRTI proxy stale (all exchanges quiet): no fair rather than one from an old price
    const tick = this.lastTick;
    if (!tick || now - tick.ts > MAX_INPUT_AGE_MS) return;
    const sigma = this.volSigma();
    if (sigma == null || !Number.isFinite(sigma)) return;

    const tLeftSec = Math.max(0, (win.endTs - now) / 1000);
    const fair = fairCents(tick.price, K, tLeftSec, sigma);
    if (!Number.isFinite(fair)) return;
    // Polymarket quote stale / down: edge = null rather than fair minus a frozen mid
    const odds = this.lastOdds;
    const upMid = odds && odds.windowId === win.id && now - odds.ts <= MAX_INPUT_AGE_MS ? odds.upMid : null;
    const snap: FairSnap = { ts: now, windowId: win.id, fair, sigma, tLeftSec, edge: edgeCents(fair, upMid) };
    this.lastFair = snap;

    const rec = this.lastFairRecord;
    if (!rec || rec.windowId !== win.id || !sameBucket(now, rec.ts)) {
      this.recorder.addFair(snap);
      this.lastFairRecord = { windowId: win.id, ts: now };
    }
    this.broadcast({ type: "fair", data: snap });
    this.emit("fair", snap);
  }

  // ------------------------------------------------------------------ Up book depth

  /** Current window's Up book from GET /v1/markets/{slug}/book; flipped when Up is the short side. */
  private async fetchBook(): Promise<BookLevels | null> {
    const win = this.tracker.current();
    if (!this.books || !win) return null;
    const { slug, side } = parseInstrument(win.upInstrument);
    const raw = await this.books.getBook(slug);
    if (!raw || this.tracker.current()?.id !== win.id) return null;
    const lv = (l: { price: number; qty: number }) => ({ price: l.price, size: l.qty });
    const flip = (l: { price: number; qty: number }) => ({ price: Math.round((100 - l.price) * 10) / 10, size: l.qty });
    const bids = (side === "long" ? raw.bids.map(lv) : raw.asks.map(flip)).filter((l) => l.size > 0).slice(0, BOOK_DEPTH);
    const asks = (side === "long" ? raw.asks.map(lv) : raw.bids.map(flip)).filter((l) => l.size > 0).slice(0, BOOK_DEPTH);
    return { ts: Date.now(), windowId: win.id, instrument: win.upInstrument, bids, asks, state: raw.state };
  }

  private setBook(b: BookLevels): void {
    this.lastBook = b;
    this.broadcast({ type: "book", data: b });
    this.emit("book", b);
  }

  private async pollBook(force = false): Promise<void> {
    if (!this.running || !this.books || this.bookBusy) return;
    const now = Date.now();
    if (!force && now < this.nextBookAt) return;
    const win = this.tracker.current();
    if (!win || now < win.startTs || now > win.endTs) return;
    this.bookBusy = true;
    try {
      const b = await this.fetchBook();
      this.bookErrors = 0;
      this.nextBookAt = 0;
      if (b && this.running) this.setBook(b);
    } catch (err) {
      this.bookErrors++;
      const wait = Math.min(BOOK_BACKOFF_MAX_MS, BOOK_POLL_MS * 2 ** Math.min(this.bookErrors, 4));
      this.nextBookAt = Date.now() + (this.bookErrors >= 3 ? wait : 0);
      if (this.bookErrors === 3) this.log.warn({ err: (err as Error).message }, "Up book polling failing; backing off");
    } finally {
      this.bookBusy = false;
    }
  }

  // ------------------------------------------------------------------ helpers

  private hasTradingKeys(): boolean {
    return !!(this.cfg.polymarketUsKeyId && this.cfg.polymarketUsSecret);
  }

  /** sigma, or null while realized vol covers less than minVolSpanSec(lookback) of history. */
  private volSigma(): number | null {
    if (this.vol.spanSec() < minVolSpanSec(this.settings.volLookbackMin)) return null;
    return this.vol.sigma();
  }

  /**
   * Fetch recent 60s candles into the DB (INSERT OR IGNORE: live 1s bars still win in reads) so a
   * window that started while the worker was down still gets a PTB fallback, and vol warms up.
   */
  private async backfillBars(): Promise<void> {
    if (!this.fetchBars) return;
    try {
      const bars = (await this.fetchBars(BACKFILL_HOURS)).filter(
        (b) => b.d === 60 && [b.t, b.o, b.h, b.l, b.c].every(Number.isFinite),
      );
      if (!this.running) return;
      const inserted = this.db.insertBarsIgnore(bars);
      const wasWarm = this.volSigma() !== null;
      if (inserted > 0 && !wasWarm) this.rebuildVol(this.settings.volLookbackMin);
      this.log.info({ fetched: bars.length, inserted, sigma: this.volSigma() }, "start-up 60s bar back-fill done");
    } catch (err) {
      this.log.warn({ err }, "start-up bar back-fill failed");
    }
  }

  /** Windows that ended (unsettled) while the worker was down: the tracker only settles windows it discovered. */
  private startSettleBackfill(): void {
    // the built-in tracker already re-adopted these (TrackerOptions.unsettled); don't race it with weaker rules
    if (!this.settlements || this.trackerRestores) return;
    const now = Date.now();
    try {
      for (const w of this.db.unsettledWindows(now - SETTLE_GIVE_UP_MS, now)) this.pendingSettle.set(w.id, w);
    } catch (err) {
      this.log.warn({ err }, "unsettled windows query failed");
      return;
    }
    if (this.pendingSettle.size === 0) return;
    this.log.info({ windows: [...this.pendingSettle.keys()] }, "settlement back-fill pending");
    void this.settleBackfill();
    this.settleTimer = setInterval(() => void this.settleBackfill(), SETTLE_RETRY_MS);
  }

  /** One pass over pendingSettle. Public for tests. */
  async settleBackfill(now = Date.now()): Promise<void> {
    const src = this.settlements;
    if (!src || this.settleBusy) return;
    this.settleBusy = true;
    try {
      for (const w of [...this.pendingSettle.values()]) {
        if (now > w.endTs + SETTLE_GIVE_UP_MS) {
          this.pendingSettle.delete(w.id);
          this.log.warn({ id: w.id }, "settlement back-fill: giving up");
          continue;
        }
        if (now < w.endTs + SETTLE_DELAY_MS) continue;
        const { slug, side } = parseInstrument(w.upInstrument);
        try {
          const s = await src.getSettlement(slug);
          if (!s) continue;
          const result = settlementToResult(s.value, side === "long");
          if (result === null) {
            this.pendingSettle.delete(w.id);
            this.log.warn({ id: w.id, value: s.value }, "settlement back-fill: non-binary settlement; leaving window unsettled");
            continue;
          }
          // an unsettled market can answer 0: trust it only once the book is no longer open
          if (s.value <= 0.01 && src.getBook) {
            const book = await src.getBook(slug).catch(() => null);
            if (book?.state && OPEN_BOOK_STATES.has(book.state)) continue;
          }
          if (!this.pendingSettle.delete(w.id)) continue; // the tracker settled it meanwhile
          const settled: MarketWindow = { ...w, result, status: "settled" };
          this.recorder.addWindow(settled);
          this.broadcast({ type: "window", data: settled });
          this.emit("window", settled);
          this.emit("settled", settled);
          this.log.info({ id: w.id, result, value: s.value }, "settlement back-fill: window settled");
        } catch (err) {
          this.log.debug({ id: w.id, err: (err as Error).message }, "settlement back-fill: not available yet");
        }
      }
    } finally {
      this.settleBusy = false;
      if (this.pendingSettle.size === 0 && this.settleTimer) {
        clearInterval(this.settleTimer);
        this.settleTimer = null;
      }
    }
  }

  private recentWindows(): MarketWindow[] {
    try {
      const curId = this.tracker.current()?.id;
      return this.db.recentFinishedWindows(RECENT_WINDOWS + 1).filter((w) => w.id !== curId).slice(0, RECENT_WINDOWS);
    } catch (err) {
      this.log.warn({ err }, "recent windows query failed");
      return [];
    }
  }

  /** Warm RealizedVol from recorded bars over the lookback (1s live bars; seeded 60s bars fill gaps). */
  private warmVol(lookbackMin = this.settings.volLookbackMin): void {
    const now = Date.now();
    try {
      const bars = this.db.bars(now - lookbackMin * 60_000, now);
      for (const b of bars) this.vol.push(b.t + b.d * 1000 - 1, b.c);
      this.log.info({ bars: bars.length, sigma: this.vol.sigma(), spanSec: this.vol.spanSec() }, "realized vol warmed from db");
    } catch (err) {
      this.log.warn({ err }, "vol warm-up failed");
    }
  }

  private applyLookback(newMin: number, oldMin: number): void {
    this.vol.setLookback(newMin * 60);
    // data older than the old lookback is gone from the estimator: rebuild from the DB
    if (newMin > oldMin) this.rebuildVol(newMin);
  }

  private rebuildVol(lookbackMin: number): void {
    this.recorder.flush();
    this.vol = newVol(lookbackMin);
    this.warmVol(lookbackMin);
    const open = this.agg.current();
    if (open) this.vol.push(open.t, open.c);
  }

  private broadcast(msg: ServerMsg): void {
    this.emit("msg", msg);
  }

  // typed overloads for module listeners (EventEmitter semantics unchanged)
  override on<K extends keyof EngineEvents>(event: K, listener: (arg: EngineEvents[K]) => void): this;
  override on(event: string | symbol, listener: (...args: any[]) => void): this;
  override on(event: string | symbol, listener: (...args: any[]) => void): this {
    return super.on(event, listener);
  }
  override off<K extends keyof EngineEvents>(event: K, listener: (arg: EngineEvents[K]) => void): this;
  override off(event: string | symbol, listener: (...args: any[]) => void): this;
  override off(event: string | symbol, listener: (...args: any[]) => void): this {
    return super.off(event, listener);
  }
}
