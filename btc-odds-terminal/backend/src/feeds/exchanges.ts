// Exchange WebSocket adapters for the BTC "BRTI proxy" feed.
//
// Documentation (message formats below follow these pages):
//  - Coinbase Exchange WebSocket feed (public, unauthenticated):
//      https://docs.cdp.coinbase.com/exchange/websocket-feed/overview
//      https://docs.cdp.coinbase.com/exchange/websocket-feed/channels  (ticker, heartbeat)
//      endpoint wss://ws-feed.exchange.coinbase.com
//  - Kraken WebSocket API v2:
//      https://docs.kraken.com/api/docs/websocket-v2/ticker
//      https://docs.kraken.com/api/docs/websocket-v2/heartbeat
//      https://docs.kraken.com/api/docs/websocket-v2/ping
//      endpoint wss://ws.kraken.com/v2
//  - Bitstamp WebSocket API v2:
//      https://www.bitstamp.net/websocket/v2/
//      endpoint wss://ws.bitstamp.net
//
// NOTE: the sandbox that authored this file could not reach those doc pages (egress 403 / no DNS),
// so the formats were written from memory of the docs, not re-verified live. The parsers are
// deliberately tolerant (unknown messages are ignored, never thrown on).

import { EventEmitter } from "node:events";
import WebSocket from "ws";
import type { ExchangeId } from "../../../shared/protocol.ts";
import type { Logger } from "../log.ts";

export interface ExchangeTick {
  exchange: ExchangeId;
  /** receive time, epoch ms (local clock, so staleness is immune to exchange clock skew) */
  ts: number;
  price: number;
}

export interface ParseResult {
  /** price updates found in the message */
  ticks: ExchangeTick[];
  /** message proves the connection is alive (data, heartbeat, subscription ack, pong) */
  alive: boolean;
  /** exchange asked us to reconnect */
  reconnect?: boolean;
  /** exchange reported an error / rejected our subscription: reconnect (with backoff) to resubscribe */
  error?: string;
  /** text to send back on the socket (app-level ping reply) */
  reply?: string;
}

export interface ExchangeSpec {
  id: ExchangeId;
  url: string;
  /** messages to send after the socket opens */
  subscribe(): string[];
  /** parse one raw text frame; must never throw */
  parse(raw: string, now: number): ParseResult;
  /** optional application-level ping text, sent every pingEveryMs */
  ping?(): string;
}

const NOTHING: ParseResult = { ticks: [], alive: false };

function toPrice(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(n) && n > 0 ? n : null;
}

function errorText(...parts: unknown[]): string {
  const text = parts.filter((p) => typeof p === "string" && p).join(": ");
  return text || "unspecified error";
}

function safeJson(raw: string): any {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- Coinbase
// Subscribe: {"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker","heartbeat"]}
// Ticker:    {"type":"ticker","sequence":..,"product_id":"BTC-USD","price":"21934.5","open_24h":"..",
//             "volume_24h":"..","low_24h":"..","high_24h":"..","volume_30d":"..","best_bid":"..",
//             "best_bid_size":"..","best_ask":"..","best_ask_size":"..","side":"buy",
//             "time":"2022-10-19T23:28:22.061769Z","trade_id":..,"last_size":".."}
// Heartbeat: {"type":"heartbeat","sequence":..,"last_trade_id":..,"product_id":"BTC-USD","time":".."}
export function parseCoinbase(raw: string, now: number): ParseResult {
  const m = safeJson(raw);
  if (!m || typeof m !== "object") return NOTHING;
  switch (m.type) {
    case "ticker": {
      if (m.product_id !== "BTC-USD") return { ticks: [], alive: true };
      const price = toPrice(m.price);
      return { ticks: price === null ? [] : [{ exchange: "coinbase", ts: now, price }], alive: true };
    }
    case "heartbeat":
    case "subscriptions":
      return { ticks: [], alive: true };
    case "error": // {"type":"error","message":"Failed to subscribe","reason":"..."}
      return { ticks: [], alive: true, error: errorText(m.message, m.reason) };
    default:
      return NOTHING;
  }
}

export const coinbaseSpec: ExchangeSpec = {
  id: "coinbase",
  url: "wss://ws-feed.exchange.coinbase.com",
  parse: parseCoinbase,
  subscribe: () => [JSON.stringify({ type: "subscribe", product_ids: ["BTC-USD"], channels: ["ticker", "heartbeat"] })],
};

// ---------------------------------------------------------------- Kraken v2
// Subscribe: {"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD"]}}
// Update:    {"channel":"ticker","type":"update","data":[{"symbol":"BTC/USD","bid":..,"bid_qty":..,"ask":..,
//             "ask_qty":..,"last":..,"volume":..,"vwap":..,"low":..,"high":..,"change":..,"change_pct":..,
//             "timestamp":"2023-10-06T17:35:55.440295Z"}]}   (type "snapshot" on subscribe, same shape)
// Heartbeat: {"channel":"heartbeat"} (about 1/s). Ping: {"method":"ping"} -> {"method":"pong",...}
// Ack:       {"method":"subscribe","result":{"channel":"ticker","symbol":"BTC/USD"},"success":true,...}
// Rejected:  {"method":"subscribe","error":"Currency pair not supported","success":false,...}
export function parseKraken(raw: string, now: number): ParseResult {
  const m = safeJson(raw);
  if (!m || typeof m !== "object") return NOTHING;
  if (m.method === "subscribe" && m.success === false) return { ticks: [], alive: true, error: errorText(m.error) };
  if (m.channel === "heartbeat" || m.method === "pong" || m.channel === "status") return { ticks: [], alive: true };
  if (m.method === "subscribe") return { ticks: [], alive: true };
  if (m.channel === "ticker" && Array.isArray(m.data)) {
    const ticks: ExchangeTick[] = [];
    for (const d of m.data) {
      if (d && d.symbol === "BTC/USD") {
        const price = toPrice(d.last);
        if (price !== null) ticks.push({ exchange: "kraken", ts: now, price });
      }
    }
    return { ticks, alive: true };
  }
  return NOTHING;
}

export const krakenSpec: ExchangeSpec = {
  id: "kraken",
  url: "wss://ws.kraken.com/v2",
  parse: parseKraken,
  subscribe: () => [JSON.stringify({ method: "subscribe", params: { channel: "ticker", symbol: ["BTC/USD"] } })],
  ping: () => JSON.stringify({ method: "ping" }),
};

// ---------------------------------------------------------------- Bitstamp v2
// Subscribe: {"event":"bts:subscribe","data":{"channel":"live_trades_btcusd"}}
// Ack:       {"event":"bts:subscription_succeeded","channel":"live_trades_btcusd","data":{}}
// Trade:     {"data":{"id":..,"timestamp":"1662629036","amount":0.01,"amount_str":"0.01000000",
//             "price":19000,"price_str":"19000","type":0,"microtimestamp":"1662629036532000",
//             "buy_order_id":..,"sell_order_id":..},"channel":"live_trades_btcusd","event":"trade"}
// Server may send {"event":"bts:request_reconnect",...} -> we must reconnect.
// Error:     {"event":"bts:error","channel":"","data":{"code":null,"message":"Bad subscription string."}}
export function parseBitstamp(raw: string, now: number): ParseResult {
  const m = safeJson(raw);
  if (!m || typeof m !== "object") return NOTHING;
  switch (m.event) {
    case "trade": {
      if (m.channel !== "live_trades_btcusd") return { ticks: [], alive: true };
      const price = toPrice(m.data?.price_str ?? m.data?.price);
      return { ticks: price === null ? [] : [{ exchange: "bitstamp", ts: now, price }], alive: true };
    }
    case "bts:request_reconnect":
      return { ticks: [], alive: true, reconnect: true };
    case "bts:error":
      return { ticks: [], alive: true, error: errorText(m.data?.message) };
    case "bts:subscription_succeeded":
    case "bts:heartbeat":
      return { ticks: [], alive: true };
    default:
      return NOTHING;
  }
}

export const bitstampSpec: ExchangeSpec = {
  id: "bitstamp",
  url: "wss://ws.bitstamp.net",
  parse: parseBitstamp,
  subscribe: () => [JSON.stringify({ event: "bts:subscribe", data: { channel: "live_trades_btcusd" } })],
};

export const EXCHANGE_SPECS: Record<ExchangeId, ExchangeSpec> = {
  coinbase: coinbaseSpec,
  kraken: krakenSpec,
  bitstamp: bitstampSpec,
};

// ---------------------------------------------------------------- connection manager
export const WATCHDOG_MS = 10_000;
/**
 * Reconnect when the socket stays up (pongs/heartbeats) but no price arrives for this long: catches a
 * subscription that was silently ignored. Long enough not to churn on a quiet venue between trades.
 */
export const NO_PRICE_RECONNECT_MS = 120_000;
const PING_EVERY_MS = 2_000;
const BACKOFF_MIN_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;

export interface FeedTimings {
  /** reconnect after this long without any frame (data, heartbeat, pong) */
  watchdogMs: number;
  /** reconnect after this long connected without a price tick */
  noPriceMs: number;
  pingEveryMs: number;
  /** reconnect delay for the given 0-based attempt */
  backoff(attempt: number): number;
}

/** Exponential backoff 1s -> 30s with +-25% jitter. attempt is 0-based. */
export function backoffMs(attempt: number, rand: () => number = Math.random): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.max(0, attempt));
  const jitter = 1 + (rand() * 2 - 1) * 0.25;
  return Math.round(Math.min(BACKOFF_MAX_MS, base * jitter));
}

/**
 * One exchange connection. Emits:
 *  - "tick"  (ExchangeTick) on each price update
 *  - "alive" (exchange, ts)  on any liveness message (heartbeat/pong/data)
 *  - "state" (exchange, connected:boolean, detail?:string)
 * Every reconnect (close, watchdog, server request, exchange error) goes through the exponential backoff;
 * the attempt counter only resets once a price tick proves the subscription works, so a venue that
 * accepts and then drops us (or rejects the subscribe) is retried 1s, 2s, 4s ... 30s, not every second.
 */
export class ExchangeFeed extends EventEmitter {
  private ws: WebSocket | null = null;
  private stopped = true;
  private attempt = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private watchdog: NodeJS.Timeout | null = null;
  private pinger: NodeJS.Timeout | null = null;
  private lastActivity = 0;
  private lastTickAt = 0;
  private readonly log: Logger;
  private readonly t: FeedTimings;

  constructor(readonly spec: ExchangeSpec, logger: Logger, timings: Partial<FeedTimings> = {}) {
    super();
    this.log = logger.child({ feed: spec.id });
    this.t = { watchdogMs: WATCHDOG_MS, noPriceMs: NO_PRICE_RECONNECT_MS, pingEveryMs: PING_EVERY_MS, backoff: backoffMs, ...timings };
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.teardownSocket();
    this.emit("state", this.spec.id, false, "stopped");
  }

  private clearTimers(): void {
    if (this.watchdog) clearInterval(this.watchdog);
    if (this.pinger) clearInterval(this.pinger);
    this.watchdog = this.pinger = null;
  }

  private teardownSocket(): void {
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;
    ws.removeAllListeners();
    ws.on("error", () => {}); // swallow late errors from a socket we abandoned
    try {
      ws.terminate();
    } catch {
      /* ignore */
    }
  }

  private connect(): void {
    if (this.stopped) return;
    this.clearTimers();
    this.teardownSocket();
    this.log.info({ url: this.spec.url }, "connecting");
    const ws = new WebSocket(this.spec.url, { handshakeTimeout: 8000, perMessageDeflate: false });
    this.ws = ws;
    this.lastActivity = Date.now();

    ws.on("open", () => {
      if (this.ws !== ws) return;
      this.lastActivity = this.lastTickAt = Date.now();
      for (const msg of this.spec.subscribe()) ws.send(msg);
      this.emit("state", this.spec.id, true);
      this.log.info("connected, subscribed");
      const checkEveryMs = Math.min(1000, this.t.watchdogMs / 4, this.t.noPriceMs / 4);
      this.watchdog = setInterval(() => {
        const now = Date.now();
        if (now - this.lastActivity > this.t.watchdogMs) {
          this.log.warn({ silentMs: now - this.lastActivity }, "watchdog: silence, reconnecting");
          this.scheduleReconnect("watchdog silence");
        } else if (now - this.lastTickAt > this.t.noPriceMs) {
          this.log.warn({ noPriceMs: now - this.lastTickAt }, "watchdog: no price data, resubscribing");
          this.scheduleReconnect("no price data");
        }
      }, checkEveryMs);
      // always keep the connection exercised: app-level ping if the exchange has one, else a WS ping frame
      this.pinger = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        try {
          if (this.spec.ping) ws.send(this.spec.ping());
          else ws.ping();
        } catch {
          /* ignore */
        }
      }, this.t.pingEveryMs);
    });

    ws.on("pong", () => {
      if (this.ws !== ws) return;
      this.onAlive();
    });
    ws.on("ping", (data) => {
      if (this.ws !== ws) return;
      try {
        ws.pong(data);
      } catch {
        /* ignore */
      }
      this.onAlive();
    });

    ws.on("message", (data) => {
      if (this.ws !== ws) return;
      const now = Date.now();
      let res: ParseResult;
      try {
        res = this.spec.parse(data.toString(), now);
      } catch (err) {
        this.log.warn({ err }, "parse threw");
        return;
      }
      if (res.alive || res.ticks.length) this.onAlive(now);
      if (res.ticks.length) {
        this.lastTickAt = now;
        this.attempt = 0; // prices are flowing: the connection is proven, reset the backoff
      }
      if (res.reply && ws.readyState === WebSocket.OPEN) ws.send(res.reply);
      for (const t of res.ticks) this.emit("tick", t);
      if (res.error) this.scheduleReconnect(`exchange error: ${res.error}`);
      else if (res.reconnect) this.scheduleReconnect("server requested reconnect");
    });

    ws.on("error", (err) => {
      if (this.ws !== ws) return;
      this.log.warn({ err: err.message }, "socket error");
    });
    ws.on("close", (code) => {
      if (this.ws !== ws) return;
      this.scheduleReconnect(`closed (${code})`);
    });
  }

  private onAlive(now = Date.now()): void {
    this.lastActivity = now;
    this.emit("alive", this.spec.id, now);
  }

  private scheduleReconnect(reason: string): void {
    if (this.stopped) return;
    this.clearTimers();
    this.teardownSocket();
    this.emit("state", this.spec.id, false, reason);
    if (this.reconnectTimer) return;
    const delay = this.t.backoff(this.attempt++);
    this.log.warn({ reason, delay }, "reconnect scheduled");
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}
