import { EventEmitter } from "node:events";
import type { BtcTick, ExchangeId, FeedStatus } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";
import { EXCHANGE_SPECS, ExchangeFeed } from "./exchanges.ts";
import type { ExchangeSpec, ExchangeTick } from "./exchanges.ts";

export const MAX_EMIT_HZ = 10;
const MIN_EMIT_GAP_MS = 1000 / MAX_EMIT_HZ;
const ALL_EXCHANGES: ExchangeId[] = ["coinbase", "kraken", "bitstamp"];

export interface LatestEntry {
  /** last trade/ticker price */
  price: number;
  /** local receive time of that price, ms (heartbeats/pongs do NOT refresh it) */
  priceTs: number;
}

export function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b);
  const n = s.length;
  if (n === 0) return NaN;
  return n % 2 ? s[(n - 1) / 2]! : (s[n / 2 - 1]! + s[n / 2]!) / 2;
}

/**
 * How long a CONNECTED exchange's last trade/ticker price keeps counting in the median. Not 3 s: Kraken
 * and Bitstamp only publish on a trade/ticker change, often less than every 3 s. With a 3 s cutoff the
 * live proxy (seen 2026-10-21) was usually ONE exchange at a time, flipping between venues a few dollars
 * apart. A disconnect still drops the venue at once (its price is cleared), so this only bounds how long
 * a quiet-but-connected venue's last print is trusted.
 */
export const PROXY_PRICE_MAX_AGE_MS = 30_000;

/**
 * Pure: median of the LIVE exchanges' latest prices. An exchange is live when it has an entry
 * (callers omit disconnected exchanges) and its price is recent: now - priceTs <= maxAgeMs.
 * Liveness messages never keep a price alive, so a frozen trade stream drops out after maxAgeMs.
 * 2 live -> mean, 1 -> itself, 0 -> null.
 */
export function computeProxy(
  latest: Partial<Record<ExchangeId, LatestEntry>>,
  now: number,
  maxAgeMs = PROXY_PRICE_MAX_AGE_MS,
): BtcTick | null {
  const sources: Partial<Record<ExchangeId, number>> = {};
  for (const [id, e] of Object.entries(latest) as [ExchangeId, LatestEntry | undefined][]) {
    if (!e || !Number.isFinite(e.price)) continue;
    if (now - e.priceTs > maxAgeMs) continue;
    sources[id] = e.price;
  }
  const prices = Object.values(sources) as number[];
  if (prices.length === 0) return null;
  return { ts: now, price: median(prices), sources };
}

/** What BtcProxy needs from a feed (ExchangeFeed; tests inject fakes). */
export interface FeedLike extends EventEmitter {
  start(): void;
  stop(): void;
}

interface FeedState {
  feed: FeedLike;
  connected: boolean;
  detail?: string;
  /** last price of the CURRENT connection; cleared on disconnect so a pre-outage price is never reused */
  price: number | null;
  priceTs: number | null;
}

export class BtcProxy extends EventEmitter {
  private readonly feeds = new Map<ExchangeId, FeedState>();
  private readonly log: Logger;
  private lastTick: BtcTick | null = null;
  private lastEmitAt = 0;
  private lastSourceKey = "";
  private emitTimer: NodeJS.Timeout | null = null;
  private housekeeping: NodeJS.Timeout | null = null;
  private running = false;

  constructor(opts: { logger: Logger; exchanges?: ExchangeId[]; createFeed?: (spec: ExchangeSpec) => FeedLike }) {
    super();
    this.log = opts.logger.child({ component: "btcProxy" });
    const createFeed = opts.createFeed ?? ((spec: ExchangeSpec) => new ExchangeFeed(spec, opts.logger));
    for (const id of opts.exchanges ?? ALL_EXCHANGES) {
      const feed = createFeed(EXCHANGE_SPECS[id]);
      const st: FeedState = { feed, connected: false, price: null, priceTs: null };
      feed.on("tick", (t: ExchangeTick) => {
        st.price = t.price;
        st.priceTs = t.ts;
        this.requestEmit();
      });
      feed.on("state", (_id: ExchangeId, connected: boolean, detail?: string) => {
        st.connected = connected;
        st.detail = detail;
        if (!connected) {
          st.price = st.priceTs = null; // a reconnect must deliver a fresh price before this source counts again
          this.requestEmit(); // drop it from the median promptly
        }
      });
      this.feeds.set(id, st);
    }
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    for (const st of this.feeds.values()) st.feed.start();
    // exchanges going stale produce no messages, so re-evaluate periodically
    this.housekeeping = setInterval(() => this.requestEmit(true), 500);
  }

  stop(): void {
    this.running = false;
    for (const st of this.feeds.values()) st.feed.stop();
    if (this.housekeeping) clearInterval(this.housekeeping);
    if (this.emitTimer) clearTimeout(this.emitTimer);
    this.housekeeping = this.emitTimer = null;
  }

  /**
   * Per-exchange health, judged on PRICE data (heartbeats/pongs only prove the socket is up):
   * live = price within PROXY_PRICE_MAX_AGE_MS (i.e. contributing to the median), stale = connected but the
   * last price is older, down = disconnected or connected without any price yet (subscription not delivering).
   */
  status(): FeedStatus[] {
    const now = Date.now();
    const out: FeedStatus[] = [];
    for (const [id, st] of this.feeds) {
      const ageMs = st.priceTs === null ? null : Math.max(0, now - st.priceTs);
      let state: FeedStatus["state"];
      let detail = st.detail;
      if (!st.connected) state = "down";
      else if (ageMs === null) {
        state = "down";
        detail = "connected, no price yet";
      } else state = ageMs > PROXY_PRICE_MAX_AGE_MS ? "stale" : "live";
      const s: FeedStatus = { id, state, lastMsgTs: st.priceTs, ageMs };
      if (detail && state === "down") s.detail = detail;
      out.push(s);
    }
    return out;
  }

  last(): BtcTick | null {
    return this.lastTick;
  }

  private snapshot(now: number): BtcTick | null {
    const latest: Partial<Record<ExchangeId, LatestEntry>> = {};
    for (const [id, st] of this.feeds) {
      if (st.connected && st.price !== null && st.priceTs !== null) latest[id] = { price: st.price, priceTs: st.priceTs };
    }
    return computeProxy(latest, now);
  }

  /** Throttled (<= 10/s) trailing-edge emit. fromTimer: only emit if the contributing source set changed. */
  private requestEmit(fromTimer = false): void {
    if (!this.running) return;
    const now = Date.now();
    const wait = this.lastEmitAt + MIN_EMIT_GAP_MS - now;
    if (wait > 0) {
      if (!this.emitTimer && !fromTimer) {
        this.emitTimer = setTimeout(() => {
          this.emitTimer = null;
          this.requestEmit();
        }, wait);
      }
      return;
    }
    const tick = this.snapshot(now);
    if (!tick) {
      this.lastSourceKey = "";
      return;
    }
    const key = Object.keys(tick.sources).sort().join(",");
    if (fromTimer && key === this.lastSourceKey) return;
    this.lastSourceKey = key;
    this.lastEmitAt = now;
    this.lastTick = tick;
    this.emit("tick", tick);
  }
}
