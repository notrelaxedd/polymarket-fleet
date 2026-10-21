// The live module end to end, offline: real Engine + Fastify server, injected fetch for the trading REST API and a
// local WebSocket server standing in for wss://api.polymarket.us/v1/ws/private.
import { EventEmitter } from "node:events";
import { createPrivateKey, createPublicKey, verify } from "node:crypto";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import { WebSocketServer, type WebSocket as WsSocket } from "ws";
import type { BtcTick, FeedStatus, MarketWindow, OddsSnap, Order, Position, ServerMsg } from "../../shared/protocol.ts";
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import { Engine, type BtcSource, type WindowSource } from "../src/engine.ts";
import { CONFIRM_MS, RECENT_FILL_MS, createLive, type LiveOptions } from "../src/live/index.ts";
import type { ExternalOrderUpdate, ModuleDeps, RemotePosition, Services, TradingService } from "../src/modules/types.ts";
import { Recorder } from "../src/recorder.ts";
import { buildServer } from "../src/server/index.ts";

const SEED = Buffer.alloc(32, 9);
const SECRET = SEED.toString("base64");
const pub = createPublicKey(createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), SEED]), format: "der", type: "pkcs8" }));

class FakeBtc extends EventEmitter implements BtcSource {
  start(): void {}
  stop(): void {}
  status(): FeedStatus[] {
    return [];
  }
  last(): BtcTick | null {
    return null;
  }
}

const now = Date.now();
const window1: MarketWindow = {
  id: "btc-1",
  title: "BTC Up or Down",
  startTs: now - 60_000,
  endTs: now + 600_000,
  priceToBeat: 67000,
  upInstrument: "btc-1:long",
  downInstrument: "btc-1:short",
  status: "live",
  result: null,
  settlePrice: null,
};

class FakeTracker extends EventEmitter implements WindowSource {
  start(): void {}
  stop(): void {}
  current(): MarketWindow | null {
    return window1;
  }
  next(): MarketWindow | null {
    return null;
  }
  lastOdds(): OddsSnap | null {
    return null;
  }
  status(): FeedStatus {
    return { id: "polymarket", state: "live", lastMsgTs: null, ageMs: null };
  }
}

const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
const amt = (d: string) => ({ value: d, currency: "USD" });

interface FakeTrading {
  external: ExternalOrderUpdate[];
  reconciled: RemotePosition[][];
  positions: Position[];
}

const livePos = (p: Partial<Position> = {}): Position => ({
  id: "p1",
  mode: "live",
  windowId: "btc-1",
  instrument: "btc-1:long",
  side: "up",
  qty: 10,
  avgCents: 40,
  costUsd: 4,
  realizedPnlUsd: 0,
  feesUsd: 0,
  status: "open",
  markCents: null,
  unrealizedPnlUsd: null,
  bracketId: null,
  openedTs: now,
  updatedTs: now,
  closedTs: null,
  ...p,
});

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0).reverse()) await f();
});

function setup(o: { keys?: boolean; handler?: (path: string, method: string) => Response; live?: LiveOptions; clock?: () => number } = {}) {
  const db = openDb(":memory:");
  const env = { DB_PATH: ":memory:", ...(o.keys === false ? {} : { POLYMARKET_US_KEY_ID: "kid", POLYMARKET_US_SECRET: SECRET }) } as NodeJS.ProcessEnv;
  const config = loadConfig(env);
  const logger = pino({ level: "silent" });
  const engine = new Engine({ config, db, recorder: new Recorder(db), logger, btc: new FakeBtc(), tracker: new FakeTracker(), fetchBars: null, settlements: null, books: null });
  const msgs: ServerMsg[] = [];
  const paths: string[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    paths.push(`${init.method} ${u.pathname}`);
    return (o.handler ?? defaultHandler)(u.pathname, String(init.method));
  }) as unknown as typeof fetch;
  const fake: FakeTrading = { external: [], reconciled: [], positions: [] };
  const trading = {
    applyExternal: (u: ExternalOrderUpdate) => fake.external.push(u),
    reconcile: (r: RemotePosition[]) => fake.reconciled.push(r),
    orders: () => [] as Order[],
    positions: () => fake.positions,
  } as unknown as TradingService;
  const deps: ModuleDeps = {
    config,
    db,
    engine,
    broadcast: (m) => msgs.push(m),
    logger,
    now: o.clock ?? Date.now,
    services: { trading } as unknown as Services,
  };
  const live = createLive(deps, { fetchImpl, privateWs: false, config: { reconcileMs: 60_000 }, ...o.live });
  const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => live.register(a), feedsIntervalMs: 60_000 });
  live.start?.();
  cleanup.push(async () => {
    await app.close();
    await live.stop?.();
    engine.stop();
    db.close();
  });
  return { engine, live, app, msgs, paths, fake };
}

function defaultHandler(path: string): Response {
  if (path === "/v1/portfolio/positions") {
    return json({
      positions: {
        "btc-1": { netPosition: "10", cost: { value: "4.00", currency: "USD" }, realized: { value: "0", currency: "USD" }, avgPx: { value: "0.40", currency: "USD" } },
        "btc-0": { netPosition: "-4", cost: { value: "2.40", currency: "USD" }, realized: { value: "0", currency: "USD" } },
        "btc-z": { netPosition: "0", cost: null, realized: { value: "1", currency: "USD" } },
      },
      eof: true,
    });
  }
  if (path === "/v1/account/balances") return json({ balances: [{ currentBalance: 100, buyingPower: 90, currency: "USD" }] });
  if (path === "/v1/orders") return json({ id: "X1" });
  if (path.startsWith("/v1/order/")) return json({ order: { id: path.split("/")[3], state: "ORDER_STATE_NEW", cumQuantity: 0 } });
  return json({}, 404);
}

async function waitFor(cond: () => boolean, ms = 3000) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error("timeout waiting for condition");
    await new Promise((r) => setTimeout(r, 10));
  }
}

const liveOrder = (p: Partial<Order> = {}): Order => ({
  id: "o1",
  clientOrderId: null,
  externalId: null,
  mode: "live",
  windowId: "btc-1",
  instrument: "btc-1:long",
  side: "up",
  action: "buy",
  type: "limit",
  limitCents: 45,
  maxSlippageCents: 2,
  qty: 10,
  notionalUsd: 4.5,
  filledQty: 0,
  avgFillCents: null,
  feesUsd: 0,
  status: "pending",
  reason: "manual",
  bracketId: null,
  positionId: null,
  error: null,
  createdTs: now,
  updatedTs: now,
  ...p,
});

describe("live module", () => {
  it("stays off in paper mode: no executor, no exchange traffic", async () => {
    const { live, paths, app } = setup();
    expect(live.service.executor()).toBeNull();
    const st = (await app.inject({ method: "GET", url: "/api/live/status" })).json();
    expect(st).toEqual({ available: true, enabled: false, connected: false, lastReconcileTs: null, balanceUsd: null, buyingPowerUsd: null, error: null });
    expect(paths).toEqual([]);
  });

  it("activates on the settings toggle, reconciles positions/balance and hands out the live executor", async () => {
    let t = Date.now();
    const { engine, live, fake, msgs } = setup({ clock: () => t });
    engine.updateSettings({ mode: "live" });
    await waitFor(() => fake.reconciled.length > 0);
    // one read is never acted on: positions the terminal does not hold wait for a second, confirming read
    expect(fake.reconciled[0]).toEqual([]);
    t += CONFIRM_MS;
    expect((await live.service.reconcileNow()).deferred).toBe(false);
    expect(fake.reconciled[1]).toEqual([
      { instrument: "btc-1:long", qty: 10, avgCents: 40 },
      { instrument: "btc-0:short", qty: 4, avgCents: 60 },
    ]);
    await waitFor(() => live.service.status().balanceUsd === 100);
    expect(live.service.status()).toMatchObject({ available: true, enabled: true, buyingPowerUsd: 90, error: null });
    expect(live.service.status().lastReconcileTs).not.toBeNull();
    expect(msgs.some((m) => m.type === "live" && m.data.enabled && m.data.balanceUsd === 100)).toBe(true);
    const ex = live.service.executor();
    expect(ex?.mode).toBe("live");

    engine.updateSettings({ mode: "paper" });
    expect(live.service.executor()).toBeNull();
    expect(live.service.status().enabled).toBe(false);
  });

  it("does not reconcile right after a fill (positions may lag), and reports reconcile errors in the status", async () => {
    let failPositions = false;
    const { engine, live, fake } = setup({
      handler: (path, method) => {
        if (path === "/v1/orders" && method === "POST") {
          return json({ id: "F1", executions: [{ id: "e1", type: "EXECUTION_TYPE_FILL", lastShares: "10", lastPx: { value: "0.45" }, order: { id: "F1", state: "ORDER_STATE_FILLED", cumQuantity: 10 } }] });
        }
        if (path === "/v1/portfolio/positions" && failPositions) return json({ message: "unauthorized" }, 401);
        return defaultHandler(path);
      },
    });
    engine.updateSettings({ mode: "live" });
    await waitFor(() => fake.reconciled.length === 1);
    const ex = live.service.executor()!;
    const r = await ex.submit(liveOrder({ type: "marketable" }), { window: window1, book: null, odds: null, feeCentsPerContract: null });
    expect(r.status).toBe("filled");
    await live.service.reconcileNow();
    expect(fake.reconciled).toHaveLength(1);

    failPositions = true;
    await new Promise((r) => setTimeout(r, 3100)); // past the quiet window
    await live.service.reconcileNow();
    expect(live.service.status().error).toMatch(/reconcile failed: 401/);
  }, 10_000);

  it("a stale read never closes a held position or re-imports a sold one on its own", async () => {
    let t = Date.now();
    let remote: Record<string, unknown> = {};
    const { engine, live, fake } = setup({
      clock: () => t,
      handler: (path) => (path === "/v1/portfolio/positions" ? json({ positions: remote, eof: true }) : defaultHandler(path)),
    });
    fake.positions = [livePos()];
    engine.updateSettings({ mode: "live" });
    await waitFor(() => fake.reconciled.length === 1);
    // the endpoint does not show our 10 yet: trading gets its own numbers (no change), not a close
    expect(fake.reconciled[0]).toEqual([{ instrument: "btc-1:long", qty: 10, avgCents: 40 }]);
    remote = { "btc-1": { netPosition: "10", avgPx: amt("0.40") } }; // it caught up: the discrepancy is dropped
    t += CONFIRM_MS;
    await live.service.reconcileNow();
    expect(fake.reconciled[1]).toEqual([{ instrument: "btc-1:long", qty: 10, avgCents: 40 }]);
    // a real close (two reads, CONFIRM_MS apart) is passed on
    remote = {};
    t += 1000;
    await live.service.reconcileNow();
    expect(fake.reconciled[2]).toEqual([{ instrument: "btc-1:long", qty: 10, avgCents: 40 }]);
    t += CONFIRM_MS;
    await live.service.reconcileNow();
    expect(fake.reconciled[3]).toEqual([]);

    // after our own exit filled, a stale read still listing the position is not imported while the fill is recent
    fake.positions = [];
    remote = { "btc-1": { netPosition: "10", avgPx: amt("0.40") } };
    const ex = live.service.executor()!;
    // simulate the exit's fill landing now (sell fills through the socket)
    (ex as unknown as { lastFillBySlug: Map<string, number> }).lastFillBySlug.set("btc-1", t);
    t += 3500;
    await live.service.reconcileNow();
    t += CONFIRM_MS;
    await live.service.reconcileNow();
    expect(fake.reconciled.at(-1)).toEqual([]);
    t += RECENT_FILL_MS;
    await live.service.reconcileNow();
    expect(fake.reconciled.at(-1)).toEqual([{ instrument: "btc-1:long", qty: 10, avgCents: 40 }]);
  });

  it("expired positions are not passed; a cost-derived short average is not re-quoted under the 'long' convention", async () => {
    let t = Date.now();
    const { engine, live, fake } = setup({
      clock: () => t,
      live: { config: { reconcileMs: 60_000, shortPriceQuote: "long" } },
      handler: (path) =>
        path === "/v1/portfolio/positions"
          ? json({
              positions: {
                "btc-0": { netPosition: "-4", cost: amt("2.40") }, // paid 60c a Down contract
                "btc-9": { netPosition: "-2", avgPx: amt("0.30") }, // quoted in long terms -> 70c Down
                "btc-8": { netPosition: "3", avgPx: amt("0.50"), expired: true },
              },
              eof: true,
            })
          : defaultHandler(path),
    });
    engine.updateSettings({ mode: "live" });
    await waitFor(() => fake.reconciled.length === 1);
    t += CONFIRM_MS;
    await live.service.reconcileNow();
    expect(fake.reconciled[1]).toEqual([
      { instrument: "btc-0:short", qty: 4, avgCents: 60 },
      { instrument: "btc-9:short", qty: 2, avgCents: 70 },
    ]);
  });

  it("an unparseable row fails the reconcile only for a traded market; balance failures surface in the status", async () => {
    let bad = "btc-x";
    let balanceDown = false;
    const { engine, live, fake } = setup({
      handler: (path) => {
        if (path === "/v1/portfolio/positions") return json({ positions: { [bad]: { netPosition: "garbage" }, "btc-0": { netPosition: "0" } }, eof: true });
        if (path === "/v1/account/balances" && balanceDown) return json({ message: "scope" }, 401);
        return defaultHandler(path);
      },
    });
    engine.updateSettings({ mode: "live" });
    await waitFor(() => fake.reconciled.length === 1);
    expect(live.service.status().error).toBeNull();
    expect(live.service.status().balanceUsd).toBe(100);

    bad = "btc-1"; // the current window's market
    const r = await live.service.reconcileNow();
    expect(r.deferred).toBe(true);
    expect(r.error).toMatch(/reconcile failed: positions: btc-1 has an unparseable netPosition/);
    expect(fake.reconciled).toHaveLength(1);

    bad = "btc-x";
    balanceDown = true;
    await live.service.reconcileNow();
    expect(live.service.status().error).toMatch(/balance read failed: 401/);
    expect(live.service.status().balanceUsd).toBe(100); // still shown, flagged by the error
    await live.service.reconcileNow();
    await live.service.reconcileNow();
    expect(live.service.status().balanceUsd).toBeNull(); // stale after repeated failures
    balanceDown = false;
    await live.service.reconcileNow();
    expect(live.service.status()).toMatchObject({ error: null, balanceUsd: 100 });
  });

  it("leaving live mode cancels our resting live orders and reports it", async () => {
    const { engine, live, paths, msgs, fake } = setup({
      handler: (path, method) => {
        if (path === "/v1/order/X1/cancel" && method === "POST") return json({});
        if (path === "/v1/order/X1") return json({ order: { id: "X1", state: "ORDER_STATE_CANCELED", cumQuantity: 0 } });
        return defaultHandler(path);
      },
    });
    engine.updateSettings({ mode: "live" });
    const ex = live.service.executor()!;
    const r = await ex.submit(liveOrder(), { window: window1, book: null, odds: null, feeCentsPerContract: null });
    expect(r).toMatchObject({ externalId: "X1", status: "open" });
    engine.updateSettings({ mode: "paper" });
    await waitFor(() => msgs.some((m) => m.type === "notice" && m.data.title === "Live orders cancelled"));
    expect(paths).toContain("POST /v1/order/X1/cancel");
    expect(paths).not.toContain("POST /v1/orders/open/cancel");
    expect(fake.external).toContainEqual({ externalId: "X1", status: "cancelled", error: null });
  });

  describe("POST /api/live/confirm", () => {
    it("409 live_unavailable without keys or outside live mode", async () => {
      const nokeys = setup({ keys: false });
      const r1 = await nokeys.app.inject({ method: "POST", url: "/api/live/confirm" });
      expect(r1.statusCode).toBe(409);
      expect(r1.json()).toMatchObject({ code: "live_unavailable" });
      expect(() => nokeys.engine.updateSettings({ mode: "live" })).toThrow(/POLYMARKET_US_KEY_ID/);

      const paper = setup();
      const r2 = await paper.app.inject({ method: "POST", url: "/api/live/confirm" });
      expect(r2.statusCode).toBe(409);
      expect(r2.json().error).toMatch(/live mode first/);
    });

    it("refuses a session when the exchange rejects the key", async () => {
      const { engine, app, live } = setup({ handler: (p) => (p === "/v1/account/balances" ? json({ message: "invalid signature" }, 401) : defaultHandler(p)) });
      engine.updateSettings({ mode: "live" });
      const r = await app.inject({ method: "POST", url: "/api/live/confirm" });
      expect(r.statusCode).toBe(409);
      expect(r.json()).toMatchObject({ code: "live_unavailable" });
      expect(r.json().error).toMatch(/rejected the API key/);
      expect(r.body).not.toContain(SECRET);
      expect(live.service.status().error).toMatch(/401/);
    });

    it("arms a session for the TTL; leaving live mode drops every session", async () => {
      let t = Date.now();
      const { engine, app, live } = setup({ clock: () => t });
      engine.updateSettings({ mode: "live" });
      const r = await app.inject({ method: "POST", url: "/api/live/confirm" });
      expect(r.statusCode).toBe(200);
      const { session, expiresTs } = r.json();
      expect(typeof session).toBe("string");
      expect(expiresTs).toBe(t + 12 * 3600_000);
      expect(live.service.isSessionConfirmed(session)).toBe(true);
      expect(live.service.isSessionConfirmed("bogus")).toBe(false);
      expect(live.service.isSessionConfirmed(null)).toBe(false);
      t += 12 * 3600_000 + 1;
      expect(live.service.isSessionConfirmed(session)).toBe(false);

      t = Date.now();
      const again = (await app.inject({ method: "POST", url: "/api/live/confirm" })).json().session;
      expect(live.service.isSessionConfirmed(again)).toBe(true);
      engine.updateSettings({ mode: "paper" });
      engine.updateSettings({ mode: "live" });
      expect(live.service.isSessionConfirmed(again)).toBe(false);
    });
  });

  describe("private WebSocket", () => {
    let wss: WebSocketServer | null = null;
    afterEach(async () => {
      await new Promise<void>((r) => (wss ? wss.close(() => r()) : r()));
      wss = null;
    });

    it("signs the upgrade, subscribes, routes executions/balances, reconnects, and closes on leaving live", async () => {
      wss = new WebSocketServer({ port: 0 });
      const port = (wss.address() as { port: number }).port;
      const upgrades: Record<string, string | string[] | undefined>[] = [];
      const subs: any[] = [];
      const socks: WsSocket[] = [];
      wss.on("connection", (sock, req) => {
        upgrades.push(req.headers);
        socks.push(sock);
        sock.on("message", (d) => subs.push(JSON.parse(d.toString())));
      });
      const { engine, live, fake } = setup({
        live: { privateWs: true, config: { privateWsUrl: `ws://127.0.0.1:${port}/v1/ws/private`, reconcileMs: 60_000 }, stream: { minBackoffMs: 20, maxBackoffMs: 50 } },
      });
      engine.updateSettings({ mode: "live" });
      await waitFor(() => subs.length >= 4);
      const h = upgrades[0]!;
      expect(h["x-pm-access-key"]).toBe("kid");
      const signed = `${h["x-pm-timestamp"]}GET/v1/ws/private`;
      expect(verify(null, Buffer.from(signed), pub, Buffer.from(String(h["x-pm-signature"]), "base64"))).toBe(true);
      expect(subs.map((s) => s.subscribe.subscriptionType).sort()).toEqual([
        "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE",
        "SUBSCRIPTION_TYPE_ORDER",
        "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT",
        "SUBSCRIPTION_TYPE_POSITION",
      ]);
      await waitFor(() => live.service.status().connected);

      // place an order, then the exchange reports its fill on the socket
      const ex = live.service.executor()!;
      const r = await ex.submit(liveOrder(), { window: window1, book: null, odds: null, feeCentsPerContract: null });
      expect(r).toMatchObject({ externalId: "X1", status: "open" });
      socks[0]!.send(
        JSON.stringify({
          requestId: subs[0].subscribe.requestId,
          subscriptionType: "SUBSCRIPTION_TYPE_ORDER",
          orderSubscriptionUpdate: {
            execution: {
              id: "ex-1",
              type: "EXECUTION_TYPE_PARTIAL_FILL",
              lastShares: "3",
              lastPx: { value: "0.45", currency: "USD" },
              commissionNotionalCollected: { value: "0.02", currency: "USD" },
              order: { id: "X1", state: "ORDER_STATE_PARTIALLY_FILLED", cumQuantity: 3 },
            },
          },
        }),
      );
      await waitFor(() => fake.external.length > 0);
      expect(fake.external[0]).toMatchObject({ externalId: "X1", status: "partial", fill: { externalId: "ex-1", qty: 3, priceCents: 45, feeUsd: 0.02 } });

      socks[0]!.send(JSON.stringify({ accountBalancesUpdate: { balanceChange: { afterBalance: { currentBalance: 55.5, buyingPower: 50, currency: "USD" } } } }));
      await waitFor(() => live.service.status().balanceUsd === 55.5);

      socks[0]!.send(JSON.stringify({ error: "subscription failed", requestId: "x" }));
      await waitFor(() => /private ws: subscription failed/.test(live.service.status().error ?? ""));
      // the next good data frame clears it (the socket is fine)
      socks[0]!.send(JSON.stringify({ accountBalancesUpdate: { balanceChange: { afterBalance: { currentBalance: 56, buyingPower: 50, currency: "USD" } } } }));
      await waitFor(() => live.service.status().error === null);

      socks[0]!.terminate();
      await waitFor(() => upgrades.length >= 2 && live.service.status().connected);
      expect(live.service.status().error).toBeNull();

      engine.updateSettings({ mode: "paper" });
      await waitFor(() => socks[1]!.readyState === socks[1]!.CLOSED || socks[1]!.readyState === socks[1]!.CLOSING);
      expect(live.service.status().connected).toBe(false);
    });
  });
});
