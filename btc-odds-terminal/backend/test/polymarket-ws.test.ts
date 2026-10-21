import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer, type WebSocket as WsSocket } from "ws";
import type { Logger } from "../src/log.ts";
import type { NormalizedBbo, NormalizedBook } from "../src/polymarket/normalize.ts";
import { MarketsStream } from "../src/polymarket/ws.ts";

const quiet = { info() {}, warn() {}, error() {}, debug() {}, child() { return quiet; } } as unknown as Logger;
// 32-byte zero seed, base64 (test key only)
const SECRET = Buffer.alloc(32).toString("base64");

describe("MarketsStream (local server)", () => {
  let wss: WebSocketServer | null = null;
  let stream: MarketsStream | null = null;
  afterEach(async () => {
    stream?.stop();
    await new Promise<void>((r) => (wss ? wss.close(() => r()) : r()));
  });

  it("signs the upgrade, subscribes to the full book (market_data), emits BBO and reconnects", async () => {
    wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as { port: number }).port;
    const headers: Record<string, string | string[] | undefined>[] = [];
    const subs: any[] = [];
    let first: WsSocket | null = null;
    wss.on("connection", (sock, req) => {
      headers.push(req.headers);
      first ??= sock;
      sock.on("message", (d) => {
        const m = JSON.parse(d.toString());
        subs.push(m);
        if (m.subscribe) {
          sock.send(JSON.stringify({
            requestId: m.subscribe.requestId,
            subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
            marketDataLite: { marketSlug: "s1", bestBid: { value: "0.40", currency: "USD" }, bestAsk: { value: "0.42", currency: "USD" } },
          }));
        }
      });
    });
    stream = new MarketsStream({ url: `ws://127.0.0.1:${port}/v1/ws/markets`, logger: quiet, auth: { keyId: "kid", secretKey: SECRET }, minBackoffMs: 50, maxBackoffMs: 200 });
    const got: NormalizedBbo[] = [];
    stream.on("bbo", (b) => got.push(b));
    stream.setSlugs(["s1"]);
    stream.start();
    await waitFor(() => got.length >= 1);
    expect(got[0]).toMatchObject({ slug: "s1", bid: 40, ask: 42 });
    expect(subs[0]).toEqual({ subscribe: { requestId: expect.any(String), subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA", marketSlugs: ["s1"] } });
    expect(headers[0]!["x-pm-access-key"]).toBe("kid");
    expect(String(headers[0]!["x-pm-signature"])).toMatch(/^[A-Za-z0-9+/]+=*$/);

    first!.terminate();
    await waitFor(() => headers.length >= 2 && got.length >= 2);
    expect(stream.connected).toBe(true);
  });

  it("full-book frames emit the book and its top of book; a crossed book is ignored", async () => {
    wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as { port: number }).port;
    const amt = (v: string) => ({ value: v, currency: "USD" });
    wss.on("connection", (sock) => {
      sock.on("message", (d) => {
        const m = JSON.parse(d.toString());
        if (!m.subscribe) return;
        // crossed first (must be dropped), then a sane book
        sock.send(JSON.stringify({ marketData: { marketSlug: "s1", bids: [{ px: amt("0.60"), qty: "5" }], offers: [{ px: amt("0.55"), qty: "5" }], state: "MARKET_STATE_OPEN" } }));
        sock.send(JSON.stringify({
          requestId: m.subscribe.requestId,
          subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA",
          marketData: {
            marketSlug: "s1",
            bids: [{ px: amt("0.61"), qty: "120" }, { px: amt("0.60"), qty: "300" }],
            offers: [{ px: amt("0.63"), qty: "80" }, { px: amt("0.64"), qty: "200" }],
            state: "MARKET_STATE_OPEN",
          },
        }));
      });
    });
    stream = new MarketsStream({ url: `ws://127.0.0.1:${port}/v1/ws/markets`, logger: quiet, auth: { keyId: "kid", secretKey: SECRET } });
    const books: NormalizedBook[] = [];
    const bbos: NormalizedBbo[] = [];
    stream.on("book", (b) => books.push(b));
    stream.on("bbo", (b) => bbos.push(b));
    stream.setSlugs(["s1"]);
    stream.start();
    await waitFor(() => books.length >= 1);
    await new Promise((r) => setTimeout(r, 50));
    expect(books).toHaveLength(1);
    expect(books[0]!.bids.map((l) => [l.price, l.qty])).toEqual([[61, 120], [60, 300]]);
    expect(books[0]!.asks.map((l) => [l.price, l.qty])).toEqual([[63, 80], [64, 200]]);
    expect(bbos).toHaveLength(1);
    expect(bbos[0]).toMatchObject({ slug: "s1", bid: 61, ask: 63 });
  });

  it("does not reset the backoff when the server accepts and immediately drops the socket", async () => {
    wss = new WebSocketServer({ port: 0 });
    const port = (wss.address() as { port: number }).port;
    let connections = 0;
    wss.on("connection", (sock) => {
      connections++;
      sock.close(1008, "unknown market");
    });
    stream = new MarketsStream({ url: `ws://127.0.0.1:${port}/v1/ws/markets`, logger: quiet, auth: null, minBackoffMs: 100, maxBackoffMs: 5000 });
    stream.setSlugs(["s1"]);
    stream.start();
    await new Promise((r) => setTimeout(r, 1500));
    // reset-on-open would reconnect every 50-100 ms (~15+ times); exponential backoff allows ~5
    expect(connections).toBeGreaterThanOrEqual(2);
    expect(connections).toBeLessThan(9);
  });
});

async function waitFor(cond: () => boolean, ms = 3000): Promise<void> {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error("timeout");
    await new Promise((r) => setTimeout(r, 10));
  }
}
