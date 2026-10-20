import { EventEmitter } from "node:events";
import { STALE_AFTER_MS } from "../../../shared/protocol.ts";
import type { BtcTick, ExchangeId, FeedStatus } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";
import { EXCHANGE_SPECS, ExchangeFeed } from "./exchanges.ts";
import type { ExchangeTick } from "./exchanges.ts";

export const MAX_EMIT_HZ = 10;
const MIN_EMIT_GAP_MS = 1000 / MAX_EMIT_HZ;
const ALL_EXCHANGES: ExchangeId[] = ["coinbase", "kraken", "bitstamp"];

export interface LatestEntry {
  /** last trade/ticker price */
  price: number;
  /** last time we heard anything (data or heartbeat) from this exchange, ms */
  msgTs: number;
}

export function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b);
  const n = s.length;
  if (n === 0) return NaN;
  return n % 2 ? s[(n - 1) / 2]! : (s[n / 2 - 1]! + s[n / 2]!) / 2;
}

/**
 * Pure: median of the LIVE exchanges' latest prices. An exchange is live when it has an entry
 * (callers omit disconnected exchanges) and now - msgTs <= staleAfterMs. 2 live -> mean, 1 -> itself, 0 -> null.
 */
export function computeProxy(
  latest: Partial<Record<ExchangeId, LatestEntry>>,
  now: number,
  staleAfterMs = STALE_AFTER_MS,
): BtcTick | null {
  const sources: Partial<Record<ExchangeId, number>> = {};
  for (const [id, e] of Object.entries(latest) as [ExchangeId, LatestEntry | undefined][]) {
    if (!e || !Number.isFinite(e.price)) continue;
    if (now - e.msgTs > staleAfterMs) continue;
    sources[id] = e.price;
  }
  const prices = Object.values(sources) as number[];
  if (prices.length === 0) return null;
  return { ts: now, price: median(prices), sources };
}

interface FeedState {
  feed: ExchangeFeed;
  connected: boolean;
  detail?: string;
  price: number | null;
  lastMsgTs: number | null;
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

  constructor(opts: { logger: Logger; exchanges?: ExchangeId[] }) {
    super();
    this.log = opts.logger.child({ component: "btcProxy" });
    for (const id of opts.exchanges ?? ALL_EXCHANGES) {
      const feed = new ExchangeFeed(EXCHANGE_SPECS[id], opts.logger);
      const st: FeedState = { feed, connected: false, price: null, lastMsgTs: null };
      feed.on("tick", (t: ExchangeTick) => {
        st.price = t.price;
        st.lastMsgTs = t.ts;
        this.requestEmit();
      });
      feed.on("alive", (_id: ExchangeId, ts: number) => {
        st.lastMsgTs = ts;
      });
      feed.on("state", (_id: ExchangeId, connected: boolean, detail?: string) => {
        st.connected = connected;
        st.detail = detail;
        if (!connected) this.requestEmit(); // drop it from the median promptly
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

  status(): FeedStatus[] {
    const now = Date.now();
    const out: FeedStatus[] = [];
    for (const [id, st] of this.feeds) {
      const ageMs = st.lastMsgTs === null ? null : Math.max(0, now - st.lastMsgTs);
      let state: FeedStatus["state"];
      if (!st.connected || st.lastMsgTs === null) state = "down";
      else state = ageMs! > STALE_AFTER_MS ? "stale" : "live";
      const s: FeedStatus = { id, state, lastMsgTs: st.lastMsgTs, ageMs };
      if (st.detail && state === "down") s.detail = st.detail;
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
      if (st.connected && st.price !== null && st.lastMsgTs !== null) latest[id] = { price: st.price, msgTs: st.lastMsgTs };
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
