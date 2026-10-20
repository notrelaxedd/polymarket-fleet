import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer } from "ws";
import type { AddressInfo } from "node:net";
import { ExchangeFeed, parseBitstamp } from "../src/feeds/index.ts";
import type { ExchangeSpec, ExchangeTick } from "../src/feeds/index.ts";
import { log } from "../src/log.ts";

let wss: WebSocketServer | null = null;
afterEach(() => wss?.close());

describe("ExchangeFeed against a local mock server", () => {
  it("subscribes, emits ticks, and reconnects after bts:request_reconnect", async () => {
    wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as AddressInfo).port;
    let conns = 0;
    const subs: string[] = [];
    wss.on("connection", (s) => {
      conns++;
      s.on("message", (m) => {
        subs.push(m.toString());
        s.send(JSON.stringify({ data: { price_str: "100.5" }, channel: "live_trades_btcusd", event: "trade" }));
        if (conns === 1) s.send(JSON.stringify({ event: "bts:request_reconnect" }));
      });
    });
    const spec: ExchangeSpec = {
      id: "bitstamp",
      url: `ws://127.0.0.1:${port}`,
      subscribe: () => ["sub"],
      parse: parseBitstamp,
    };
    const feed = new ExchangeFeed(spec, log);
    const ticks: ExchangeTick[] = [];
    feed.on("tick", (t) => ticks.push(t));
    feed.start();
    await new Promise((r) => setTimeout(r, 800));
    feed.stop();
    expect(conns).toBe(2);
    expect(subs).toEqual(["sub", "sub"]);
    expect(ticks.length).toBe(2);
    expect(ticks[0]!.price).toBe(100.5);
  });
});
