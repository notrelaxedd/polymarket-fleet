import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer } from "ws";
import type { WebSocket } from "ws";
import type { AddressInfo } from "node:net";
import { ExchangeFeed, krakenSpec, parseBitstamp } from "../src/feeds/index.ts";
import type { ExchangeSpec, ExchangeTick, FeedTimings } from "../src/feeds/index.ts";
import { log } from "../src/log.ts";

let wss: WebSocketServer | null = null;
let feed: ExchangeFeed | null = null;
afterEach(() => {
  feed?.stop();
  feed = null;
  wss?.close();
  wss = null;
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const TRADE = JSON.stringify({ data: { price_str: "100.5" }, channel: "live_trades_btcusd", event: "trade" });

/** Start a mock server; returns its port and a connection counter. */
function serve(onConn: (s: WebSocket, n: number) => void): { port: number; conns: () => number } {
  let n = 0;
  wss = new WebSocketServer({ port: 0 });
  wss.on("connection", (s) => onConn(s, ++n));
  return { port: (wss.address() as AddressInfo).port, conns: () => n };
}

/** Feed with fast timings; records every backoff attempt and every state change. */
function startFeed(spec: Partial<ExchangeSpec> & { url: string }, timings: Partial<FeedTimings> = {}) {
  const attempts: number[] = [];
  const states: Array<[boolean, string | undefined]> = [];
  const ticks: ExchangeTick[] = [];
  const full: ExchangeSpec = { id: "bitstamp", subscribe: () => ["sub"], parse: parseBitstamp, ...spec };
  feed = new ExchangeFeed(full, log, {
    backoff: (a) => {
      attempts.push(a);
      return 20;
    },
    ...timings,
  });
  feed.on("tick", (t) => ticks.push(t));
  feed.on("state", (_id, c: boolean, d?: string) => states.push([c, d]));
  feed.start();
  return { attempts, states, ticks };
}

describe("ExchangeFeed against a local mock server", () => {
  it("subscribes, emits ticks, and reconnects after bts:request_reconnect", async () => {
    const subs: string[] = [];
    const srv = serve((s, n) => {
      s.on("message", (m) => {
        subs.push(m.toString());
        s.send(TRADE);
        if (n === 1) s.send(JSON.stringify({ event: "bts:request_reconnect" }));
      });
    });
    const { ticks, attempts } = startFeed({ url: `ws://127.0.0.1:${srv.port}` });
    await sleep(400);
    expect(srv.conns()).toBe(2);
    expect(subs).toEqual(["sub", "sub"]);
    expect(ticks.length).toBe(2);
    expect(ticks[0]!.price).toBe(100.5);
    expect(attempts).toEqual([0]); // goes through backoff (min ~1s in prod), never a 0ms loop
  });

  it("accept-then-close servers get a growing backoff; a price tick resets it", async () => {
    const srv = serve((s, n) => {
      if (n <= 3) s.close();
      else s.on("message", () => (s.send(TRADE), s.close()));
    });
    const { attempts } = startFeed({ url: `ws://127.0.0.1:${srv.port}` });
    for (let i = 0; i < 100 && attempts.length < 5; i++) await sleep(20);
    expect(attempts.slice(0, 5)).toEqual([0, 1, 2, 0, 0]); // 3 bare closes escalate; once trades flow it resets
  });

  it("exchange error (rejected subscribe) reconnects with backoff", async () => {
    const srv = serve((s) => s.on("message", () => s.send(JSON.stringify({ event: "bts:error", data: { message: "Bad subscription string." } }))));
    const { attempts, states } = startFeed({ url: `ws://127.0.0.1:${srv.port}` });
    for (let i = 0; i < 100 && attempts.length < 3; i++) await sleep(20);
    expect(attempts.slice(0, 3)).toEqual([0, 1, 2]);
    expect(states).toContainEqual([false, "exchange error: Bad subscription string."]);
  });

  it("watchdog reconnects a silent socket", async () => {
    const srv = serve(() => {}); // never sends; app-level ping below gets no answer, so no pongs either
    const { states } = startFeed({ url: `ws://127.0.0.1:${srv.port}`, ping: () => "noop" }, { watchdogMs: 150, pingEveryMs: 50 });
    for (let i = 0; i < 50 && srv.conns() < 2; i++) await sleep(20);
    expect(srv.conns()).toBeGreaterThanOrEqual(2);
    expect(states).toContainEqual([false, "watchdog silence"]);
  });

  it("pongs keep the socket alive but a feed with no prices is resubscribed", async () => {
    const srv = serve(() => {}); // ws server auto-answers ping frames with pongs
    const { states } = startFeed({ url: `ws://127.0.0.1:${srv.port}` }, { watchdogMs: 150, pingEveryMs: 30, noPriceMs: 300 });
    for (let i = 0; i < 50 && srv.conns() < 2; i++) await sleep(20);
    expect(srv.conns()).toBeGreaterThanOrEqual(2);
    expect(states).toContainEqual([false, "no price data"]);
    expect(states).not.toContainEqual([false, "watchdog silence"]);
  });

  it("kraken sends its app-level ping", async () => {
    const got: string[] = [];
    const srv = serve((s) => s.on("message", (m) => got.push(m.toString())));
    startFeed({ ...krakenSpec, url: `ws://127.0.0.1:${srv.port}` }, { pingEveryMs: 30 });
    for (let i = 0; i < 50 && got.length < 3; i++) await sleep(20);
    expect(JSON.parse(got[0]!)).toEqual({ method: "subscribe", params: { channel: "ticker", symbol: ["BTC/USD"] } });
    expect(got.slice(1, 3).map((m) => JSON.parse(m))).toEqual([{ method: "ping" }, { method: "ping" }]);
  });
});
