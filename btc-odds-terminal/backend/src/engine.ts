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
import type { Config } from "./config.ts";
import type { TerminalDb } from "./db/index.ts";
import { loadSettings, saveSettings, SettingsPatchSchema, type StoredSettings } from "./db/settings.ts";
import { edgeCents, fairCents, RealizedVol } from "./fair/index.ts";
import { BarAggregator, BtcProxy } from "./feeds/index.ts";
import type { Logger } from "./log.ts";
import { FEE_SCHEDULE, MarketTracker } from "./polymarket/index.ts";
import type { Recorder } from "./recorder.ts";

/** Min gap between fair recomputes. */
export const FAIR_MIN_GAP_MS = 250;
/** Min gap between persisted fair / odds rows (per window). */
export const RECORD_GAP_MS = 1000;
/** In-memory ring of recent 1s bars (for PTB fallback lookups). */
const RING_MAX = 2 * 3600;
/** A BTC tick older than this is too stale to compute fair value from. */
const MAX_TICK_AGE_MS = 15_000;
/** How many finished windows the snapshot carries. */
const RECENT_WINDOWS = 20;

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

  private ring: Bar[] = [];
  private lastTick: BtcTick | null = null;
  private lastOdds: OddsSnap | null = null;
  private lastFair: FairSnap | null = null;
  private lastFairAt = 0;
  private lastFairRecord: { windowId: string; ts: number } | null = null;
  private lastOddsRecord: { windowId: string; ts: number } | null = null;
  private fairTimer: NodeJS.Timeout | null = null;
  private clock: NodeJS.Timeout | null = null;
  private running = false;

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
    this.vol = new RealizedVol(this.settings.volLookbackMin * 60);
    this.btc = deps.btc ?? new BtcProxy({ logger: deps.logger });
    this.tracker =
      deps.tracker ??
      new MarketTracker({
        logger: deps.logger,
        pollMs: this.cfg.polyPollMs,
        priceAt: (ts: number) => this.priceAt(ts),
        config: { gatewayUrl: this.cfg.polyGatewayUrl },
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
    this.log.info(
      { settings: this.settings, sigma: this.vol.sigma(), volSamples: this.vol.count() },
      "engine started",
    );
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    if (this.clock) clearInterval(this.clock);
    if (this.fairTimer) clearTimeout(this.fairTimer);
    this.clock = this.fairTimer = null;
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
    return {
      serverTime: Date.now(),
      settings: this.getSettings(),
      current: this.tracker.current(),
      next: this.tracker.next(),
      recent: this.recentWindows(),
      lastTick: this.lastTick ?? this.btc.last(),
      lastOdds: this.lastOdds ?? this.tracker.lastOdds(),
      lastFair: this.lastFair,
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
      if (!this.cfg.polymarketUsKeyId || !this.cfg.polymarketUsSecret) {
        throw new SettingsError("live mode requires POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET in the backend .env");
      }
    }
    const next: StoredSettings = { ...this.settings };
    if (p.mode !== undefined) next.mode = p.mode;
    if (p.feeCentsPerContract !== undefined) next.feeCentsPerContract = p.feeCentsPerContract;
    if (p.volLookbackMin !== undefined && p.volLookbackMin !== this.settings.volLookbackMin) {
      next.volLookbackMin = p.volLookbackMin;
      this.applyLookback(p.volLookbackMin, this.settings.volLookbackMin);
    }
    saveSettings(this.db, next);
    this.settings = next;
    const s = this.getSettings();
    this.log.info({ settings: s }, "settings updated");
    this.broadcast({ type: "settings", data: s });
    this.requestFair();
    return s;
  }

  /**
   * BTC proxy price at ts for the PTB fallback: close of the recorded 1s bar whose
   * close time (t + 1s) is nearest ts, from the in-memory ring, else the DB.
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
    if (bestDist > tolMs) {
      try {
        consider(this.db.nearestBar1s(ts, tolMs));
      } catch (err) {
        this.log.warn({ err }, "priceAt db lookup failed");
      }
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
    if (!rec || rec.windowId !== odds.windowId || odds.ts - rec.ts >= RECORD_GAP_MS) {
      this.recorder.addOdds(odds);
      this.lastOddsRecord = { windowId: odds.windowId, ts: odds.ts };
    }
    this.broadcast({ type: "odds", data: odds });
    this.requestFair();
  };

  private readonly onWindow = (w: MarketWindow): void => {
    if (!w) return;
    this.recorder.addWindow(w);
    this.broadcast({ type: "window", data: w });
    if (w.id === this.tracker.current()?.id) this.requestFair();
  };

  /** The tracker also emits "window" for a settlement; just make sure it is persisted and log it. */
  private readonly onSettled = (w: MarketWindow): void => {
    if (!w) return;
    this.recorder.addWindow(w);
    this.log.info({ id: w.id, result: w.result, settlePrice: w.settlePrice, ptb: w.priceToBeat }, "window settled");
  };

  private readonly onRollover = (r: { current: MarketWindow | null; next: MarketWindow | null }): void => {
    if (r.current) this.recorder.addWindow(r.current);
    if (r.next) this.recorder.addWindow(r.next);
    if (this.lastFair && this.lastFair.windowId !== r.current?.id) this.lastFair = null;
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
    const tick = this.lastTick;
    if (!tick || now - tick.ts > MAX_TICK_AGE_MS) return;
    const sigma = this.vol.sigma();
    if (sigma == null || !Number.isFinite(sigma)) return;

    const tLeftSec = Math.max(0, (win.endTs - now) / 1000);
    const fair = fairCents(tick.price, K, tLeftSec, sigma);
    if (!Number.isFinite(fair)) return;
    const upMid = this.lastOdds && this.lastOdds.windowId === win.id ? this.lastOdds.upMid : null;
    const snap: FairSnap = { ts: now, windowId: win.id, fair, sigma, tLeftSec, edge: edgeCents(fair, upMid) };
    this.lastFair = snap;

    const rec = this.lastFairRecord;
    if (!rec || rec.windowId !== win.id || now - rec.ts >= RECORD_GAP_MS) {
      this.recorder.addFair(snap);
      this.lastFairRecord = { windowId: win.id, ts: now };
    }
    this.broadcast({ type: "fair", data: snap });
  }

  // ------------------------------------------------------------------ helpers

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
      this.log.info({ bars: bars.length, sigma: this.vol.sigma() }, "realized vol warmed from db");
    } catch (err) {
      this.log.warn({ err }, "vol warm-up failed");
    }
  }

  private applyLookback(newMin: number, oldMin: number): void {
    this.vol.setLookback(newMin * 60);
    if (newMin > oldMin) {
      // data older than the old lookback is gone from the estimator: rebuild from the DB
      this.recorder.flush();
      this.vol = new RealizedVol(newMin * 60);
      this.warmVol(newMin);
      const open = this.agg.current();
      if (open) this.vol.push(open.t, open.c);
    }
  }

  private broadcast(msg: ServerMsg): void {
    this.emit("msg", msg);
  }
}
