import { EventEmitter } from "node:events";
import type {
  Bar,
  BtcTick,
  FairSnap,
  FeedStatus,
  MarketWindow,
  OddsSnap,
  ServerMsg,
  Settings,
  StateSnapshot,
} from "../../shared/protocol.ts";
import { STALE_AFTER_MS } from "../../shared/protocol.ts";
import type { Config } from "./config.ts";
import type { TerminalDb } from "./db/index.ts";
import { loadSettings, saveSettings, SettingsPatchSchema, type StoredSettings } from "./db/settings.ts";
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
    if (this.settings.mode === "live" && !this.hasTradingKeys()) {
      this.log.warn("stored mode is live but POLYMARKET_US_KEY_ID/SECRET are missing: starting in paper");
      this.settings = { ...this.settings, mode: "paper" };
    }
    this.fetchBars = deps.fetchBars !== undefined ? deps.fetchBars : fetchRecentBars;
    this.settlements =
      deps.settlements !== undefined ? deps.settlements : new PolymarketClient({ baseUrl: this.cfg.polyGatewayUrl });
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
    this.clock = this.fairTimer = this.settleTimer = null;
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
    return {
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
    return { ...this.settings, feeNote: this.feeNote };
  }

  /** Apply a partial settings update. Throws SettingsError (400) on invalid input. */
  updateSettings(patch: unknown): Settings {
    const parsed = SettingsPatchSchema.safeParse(patch ?? {});
    if (!parsed.success) {
      throw new SettingsError(parsed.error.issues.map((i) => `${i.path.join(".") || "body"}: ${i.message}`).join("; "));
    }
    const p = parsed.data;
    if (p.mode === "live" && this.settings.mode !== "live") {
      if (!this.hasTradingKeys()) {
        throw new SettingsError("live mode requires POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET in the backend .env");
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
    this.requestFair();
  };

  private readonly onWindow = (w: MarketWindow): void => {
    if (!w) return;
    if (w.result) this.pendingSettle.delete(w.id);
    this.recorder.addWindow(w);
    this.broadcast({ type: "window", data: w });
    if (w.id === this.tracker.current()?.id) this.requestFair();
  };

  /** The tracker also emits "window" for a settlement; just make sure it is persisted and log it. */
  private readonly onSettled = (w: MarketWindow): void => {
    if (!w) return;
    this.pendingSettle.delete(w.id);
    this.recorder.addWindow(w);
    this.log.info({ id: w.id, result: w.result, settlePrice: w.settlePrice, ptb: w.priceToBeat }, "window settled");
  };

  private readonly onRollover = (r: { current: MarketWindow | null; next: MarketWindow | null }): void => {
    if (r.current) this.recorder.addWindow(r.current);
    if (r.next) this.recorder.addWindow(r.next);
    if (this.lastFair && this.lastFair.windowId !== r.current?.id) this.lastFair = null;
    if (this.lastOdds && this.lastOdds.windowId !== r.current?.id) this.lastOdds = null;
    this.log.info({ current: r.current?.id ?? null, next: r.next?.id ?? null }, "rollover");
    this.broadcast({ type: "rollover", data: { current: r.current, next: r.next } });
    this.requestFair();
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
}
