// Private WebSocket (wss://api.polymarket.us/v1/ws/private): order executions, order snapshots, position changes and
// balances. VERIFIED against the official SDK (src/websocket/{base,private,types}.ts):
//  - auth: X-PM-* headers on the HTTP upgrade, signed as "GET" + "/v1/ws/private";
//  - subscribe: {"subscribe":{"requestId","subscriptionType"}} with SUBSCRIPTION_TYPE_ORDER (updates only),
//    SUBSCRIPTION_TYPE_ORDER_SNAPSHOT (one-shot, ends with eof), SUBSCRIPTION_TYPE_POSITION (changes only, no snapshot:
//    the initial state comes from GET /v1/portfolio/positions) and SUBSCRIPTION_TYPE_ACCOUNT_BALANCE;
//  - frames: parsed by wire.ts parsePrivateFrame (current + legacy envelope keys).
// Subscriptions do not survive a reconnect: every open resubscribes and asks for a fresh order snapshot.
// Reconnect backoff 1s -> 30s with jitter; it resets only after a data frame or 30s of uptime. Repeated 401/403
// upgrade rejections (bad keys, clock skew) back off to minutes. A socket silent for idleTimeoutMs is terminated.

import { EventEmitter } from "node:events";
import WebSocket from "ws";
import type { Logger } from "../log.ts";
import { createAuthHeaders } from "../polymarket/auth.ts";
import { backoffMs } from "../polymarket/ws.ts";
import { parsePrivateFrame, type PrivateFrame } from "./wire.ts";

export const PRIVATE_SUBSCRIPTIONS = [
  "SUBSCRIPTION_TYPE_ORDER",
  "SUBSCRIPTION_TYPE_POSITION",
  "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE",
  "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT",
] as const;

export interface PrivateStreamOptions {
  url: string;
  logger: Logger;
  auth: { keyId: string; secretKey: string };
  /** clock used to sign the upgrade (e.g. corrected by the REST client's server-time offset) */
  signNow?: () => number;
  minBackoffMs?: number;
  maxBackoffMs?: number;
  /** terminate if nothing (data, heartbeat, pong) arrives for this long (default 30s) */
  idleTimeoutMs?: number;
  /** a connection that stays up this long resets the backoff (default 30s) */
  stableMs?: number;
  /** ping interval (default 10s) */
  pingMs?: number;
}

const AUTH_BACKOFF_MIN_MS = 60_000;
const AUTH_BACKOFF_MAX_MS = 10 * 60_000;

/**
 * Events: "open", "close", "frame" (PrivateFrame other than heartbeat / other), "serverError" (string).
 */
export class PrivateStream extends EventEmitter {
  private ws: WebSocket | null = null;
  private running = false;
  private attempt = 0;
  private authFailures = 0;
  private openedAt: number | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private seq = 0;
  private _connected = false;
  lastMsgTs: number | null = null;

  constructor(private readonly opts: PrivateStreamOptions) {
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
    const was = this._connected;
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
    if (was) this.emit("close");
  }

  /** Ask for a fresh one-shot order snapshot (e.g. after a suspected gap). */
  requestOrderSnapshot(): void {
    this.subscribe("SUBSCRIPTION_TYPE_ORDER_SNAPSHOT");
  }

  private send(obj: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  private subscribe(subscriptionType: string): void {
    const requestId = `terminal-${subscriptionType.replace("SUBSCRIPTION_TYPE_", "").toLowerCase()}-${++this.seq}`;
    this.send({ subscribe: { requestId, subscriptionType } });
  }

  private async connect(): Promise<void> {
    if (!this.running) return;
    const { url, logger, auth } = this.opts;
    let headers: Record<string, string>;
    try {
      const now = this.opts.signNow?.() ?? Date.now();
      headers = { ...(await createAuthHeaders(auth.keyId, auth.secretKey, "GET", new URL(url).pathname, now)) };
    } catch (err) {
      logger.error({ err: (err as Error).message }, "private ws: cannot sign the upgrade (bad secret?)");
      this.emit("serverError", "cannot sign the upgrade (check POLYMARKET_US_SECRET)");
      this.scheduleReconnect();
      return;
    }
    if (!this.running) return;
    const ws = new WebSocket(url, { headers, handshakeTimeout: 10_000 });
    this.ws = ws;

    ws.on("open", () => {
      this.authFailures = 0;
      this.openedAt = Date.now();
      this._connected = true;
      this.lastMsgTs = Date.now();
      logger.info({ url }, "private ws: connected");
      for (const t of PRIVATE_SUBSCRIPTIONS) this.subscribe(t);
      this.emit("open");
      if (this.pingTimer) clearInterval(this.pingTimer);
      const idle = this.opts.idleTimeoutMs ?? 30_000;
      this.pingTimer = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        if (this.lastMsgTs !== null && Date.now() - this.lastMsgTs > idle) {
          logger.warn("private ws: idle, reconnecting");
          ws.terminate();
          return;
        }
        try {
          ws.ping();
        } catch {
          /* closed */
        }
      }, this.opts.pingMs ?? 10_000);
    });
    ws.on("pong", () => {
      this.lastMsgTs = Date.now();
    });
    ws.on("message", (data) => {
      this.lastMsgTs = Date.now();
      const frame: PrivateFrame = parsePrivateFrame(data.toString());
      if (frame.kind === "heartbeat" || frame.kind === "other") return;
      if (frame.kind === "error") {
        logger.warn({ error: frame.error, requestId: frame.requestId }, "private ws: server error frame");
        this.emit("serverError", frame.error);
        return;
      }
      this.attempt = 0; // healthy: data flows
      this.emit("frame", frame);
    });
    ws.on("unexpected-response", (_req, res) => {
      logger.warn({ status: res.statusCode }, "private ws: upgrade rejected");
      if (res.statusCode === 401 || res.statusCode === 403) this.authFailures++;
      this.emit("serverError", `upgrade rejected: HTTP ${res.statusCode}`);
      ws.terminate();
    });
    ws.on("error", (err) => {
      logger.debug({ err: err.message }, "private ws: socket error");
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
    if (this.authFailures >= 2) delay = Math.max(delay, backoffMs(this.authFailures - 2, AUTH_BACKOFF_MIN_MS, AUTH_BACKOFF_MAX_MS));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }
}
