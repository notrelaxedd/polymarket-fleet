import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer, type WebSocket as WsSocket } from "ws";
import type { Logger } from "../src/log.ts";
import type { NormalizedBbo } from "../src/polymarket/normalize.ts";
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

  it("signs the upgrade, subscribes to market_data_lite, emits BBO and reconnects", async () => {
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
    expect(subs[0]).toEqual({ subscribe: { requestId: expect.any(String), subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", marketSlugs: ["s1"] } });
    expect(headers[0]!["x-pm-access-key"]).toBe("kid");
    expect(String(headers[0]!["x-pm-signature"])).toMatch(/^[A-Za-z0-9+/]+=*$/);

    first!.terminate();
    await waitFor(() => headers.length >= 2 && got.length >= 2);
    expect(stream.connected).toBe(true);
  });
});

async function waitFor(cond: () => boolean, ms = 3000): Promise<void> {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error("timeout");
    await new Promise((r) => setTimeout(r, 10));
  }
}
