// Markets WebSocket (wss://api.polymarket.us/v1/ws/markets) -> top-of-book updates.
// Verified against the official SDK (src/websocket/base.ts, markets.ts, types.ts):
//  - auth: the SDK always signs the upgrade (X-PM-* headers, "GET" + "/v1/ws/markets"); it refuses to open
//    the markets socket without keys, so the stream is treated as AUTH REQUIRED.
//  - subscribe:   {"subscribe":{"requestId","subscriptionType":"SUBSCRIPTION_TYPE_MARKET_DATA_LITE","marketSlugs":[...]}}
//  - unsubscribe: {"unsubscribe":{"requestId"}}
//  - frames: { requestId, subscriptionType, marketDataLite: { marketSlug, bestBid, bestAsk, lastTradePx } },
//            { marketData: { marketSlug, bids, offers, state, stats, transactTime } }, { trade: {...} },
//            { heartbeat: {} }, { requestId?, subscriptionType?, error }
//  - subscriptions do not survive a reconnect: we resubscribe on every open.
//  - backoff resets only once a connection proved healthy (a data frame, or up for `stableMs`), so a server that
//    accepts the upgrade and then drops us does not cause a ~1/s reconnect storm; repeated 401/403 upgrade
//    rejections (bad keys, clock skew) back off to minutes.

import { EventEmitter } from "node:events";
import WebSocket from "ws";
import type { Logger } from "../log.ts";
import { createAuthHeaders } from "./auth.ts";
import { bookToBbo, parseWsMessage } from "./normalize.ts";

export interface StreamOptions {
  url: string;
  logger: Logger;
  /** null = connect without auth headers */
  auth: { keyId: string; secretKey: string } | null;
  /** reconnect backoff bounds, ms (default 1000 -> 30000) */
  minBackoffMs?: number;
  maxBackoffMs?: number;
  /** terminate the socket if nothing (data, heartbeat, pong) arrives for this long (default 20s) */
  idleTimeoutMs?: number;
  /** a connection that stays up this long resets the backoff (default 30s) */
  stableMs?: number;
}

const AUTH_BACKOFF_MIN_MS = 60_000;
const AUTH_BACKOFF_MAX_MS = 10 * 60_000;

/** Exponential backoff with jitter: base * 2^attempt capped at max, then scaled into [50%, 100%]. */
export function backoffMs(attempt: number, minMs = 1000, maxMs = 30_000, rand: () => number = Math.random): number {
  const raw = Math.min(maxMs, minMs * 2 ** Math.max(0, attempt));
  return Math.max(minMs / 2, Math.round(raw * (0.5 + 0.5 * rand())));
}

/** Events: "bbo" (NormalizedBbo), "open", "close", "message" (any frame: liveness), "serverError" (string). */
export class MarketsStream extends EventEmitter {
  private ws: WebSocket | null = null;
  private running = false;
  private attempt = 0;
  private authFailures = 0;
  private openedAt: number | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private slugs: string[] = [];
  private requestSeq = 0;
  private activeRequestId: string | null = null;
  private _connected = false;
  lastMsgTs: number | null = null;

  constructor(private readonly opts: StreamOptions) {
    super();
  }

  get connected(): boolean {
    return this._connected;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    void this.connect();
  }

  stop(): void {
    this.running = false;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.reconnectTimer = null;
    this.pingTimer = null;
    const ws = this.ws;
    this.ws = null;
    this._connected = false;
    if (ws) {
      ws.removeAllListeners();
      ws.on("error", () => {});
      try {
        ws.close(1000, "OK");
      } catch {
        ws.terminate();
      }
    }
  }

  /** Replace the subscribed market slugs (current + next window). */
  setSlugs(slugs: string[]): void {
    const uniq = [...new Set(slugs.filter(Boolean))].sort();
    if (uniq.join("|") === this.slugs.join("|")) return;
    this.slugs = uniq;
    if (this._connected) this.resubscribe();
  }

  private send(obj: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  private resubscribe(): void {
    if (this.activeRequestId) this.send({ unsubscribe: { requestId: this.activeRequestId } });
    this.activeRequestId = null;
    if (this.slugs.length === 0) return;
    const requestId = `bot-bbo-${++this.requestSeq}`;
    this.activeRequestId = requestId;
    this.send({
      subscribe: { requestId, subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", marketSlugs: this.slugs },
    });
  }

  private async connect(): Promise<void> {
    if (!this.running) return;
    const { url, logger, auth } = this.opts;
    let headers: Record<string, string> = {};
    if (auth) {
      try {
        headers = { ...(await createAuthHeaders(auth.keyId, auth.secretKey, "GET", new URL(url).pathname)) };
      } catch (err) {
        logger.error({ err: (err as Error).message }, "polymarket ws: cannot sign upgrade (bad secret?)");
        this.scheduleReconnect();
        return;
      }
    }
    if (!this.running) return;
    const ws = new WebSocket(url, { headers, handshakeTimeout: 10_000 });
    this.ws = ws;

    ws.on("open", () => {
      this.authFailures = 0;
      this.openedAt = Date.now();
      this._connected = true;
      this.lastMsgTs = Date.now();
      this.activeRequestId = null;
      logger.info({ url, auth: !!auth }, "polymarket ws: connected");
      this.resubscribe();
      this.emit("open");
      if (this.pingTimer) clearInterval(this.pingTimer);
      const idle = this.opts.idleTimeoutMs ?? 20_000;
      this.pingTimer = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        if (this.lastMsgTs !== null && Date.now() - this.lastMsgTs > idle) {
          logger.warn("polymarket ws: idle, reconnecting");
          ws.terminate();
          return;
        }
        try {
          ws.ping();
        } catch {
          /* closed */
        }
      }, 5000);
    });
    ws.on("pong", () => {
      this.lastMsgTs = Date.now();
    });
    ws.on("message", (data) => {
      this.lastMsgTs = Date.now();
      const msg = parseWsMessage(data.toString());
      this.emit("message");
      if (msg.kind === "bbo" || msg.kind === "book" || msg.kind === "trade") this.attempt = 0; // healthy: data flows
      if (msg.kind === "bbo") this.emit("bbo", msg.bbo);
      else if (msg.kind === "book") this.emit("bbo", bookToBbo(msg.book));
      else if (msg.kind === "error") {
        logger.warn({ error: msg.error, requestId: msg.requestId }, "polymarket ws: server error frame");
        this.emit("serverError", msg.error);
      }
    });
    ws.on("unexpected-response", (_req, res) => {
      logger.warn({ status: res.statusCode, auth: !!auth }, "polymarket ws: upgrade rejected");
      if (res.statusCode === 401 || res.statusCode === 403) this.authFailures++;
      this.emit("serverError", `upgrade rejected: HTTP ${res.statusCode}`);
      ws.terminate();
    });
    ws.on("error", (err) => {
      logger.debug({ err: err.message }, "polymarket ws: socket error");
    });
    ws.on("close", () => {
      if (this.ws === ws) this.ws = null;
      const was = this._connected;
      this._connected = false;
      if (this.openedAt !== null && Date.now() - this.openedAt >= (this.opts.stableMs ?? 30_000)) this.attempt = 0;
      this.openedAt = null;
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = null;
      if (was) this.emit("close");
      this.scheduleReconnect();
    });
  }

  private scheduleReconnect(): void {
    if (!this.running || this.reconnectTimer) return;
    let delay = backoffMs(this.attempt++, this.opts.minBackoffMs ?? 1000, this.opts.maxBackoffMs ?? 30_000);
    // a repeated 401/403 will not fix itself in a second: back off to minutes
    if (this.authFailures >= 2) delay = Math.max(delay, backoffMs(this.authFailures - 2, AUTH_BACKOFF_MIN_MS, AUTH_BACKOFF_MAX_MS));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }
}
