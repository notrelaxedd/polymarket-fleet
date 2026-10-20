// Request / response shapes of the live REST client against the official SDK (src/resources/{orders,portfolio,
// account}.ts, src/types/*.ts). All offline: fetch is injected.
import { describe, expect, it } from "vitest";
import { LiveHttpError, LiveRestClient } from "../src/live/rest.ts";
import { centsToDollars, exactCents, mapOrderState, parseOrder, parsePrivateFrame } from "../src/live/wire.ts";

const SECRET = Buffer.alloc(32, 1).toString("base64");
const NOW = 1_760_000_000_000;

interface Call {
  method: string;
  url: string;
  path: string;
  query: URLSearchParams;
  headers: Record<string, string>;
  body: unknown;
}

function client(handler: (c: Call) => Response | Promise<Response>, now = () => NOW) {
  const calls: Call[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const c: Call = {
      method: String(init.method),
      url,
      path: u.pathname,
      query: u.searchParams,
      headers: init.headers as Record<string, string>,
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(c);
    return handler(c);
  }) as unknown as typeof fetch;
  const rest = new LiveRestClient({ baseUrl: "https://api.polymarket.us/", keyId: "kid", secretKey: SECRET, fetchImpl, now, timeoutMs: 1000 });
  return { rest, calls };
}

const json = (v: unknown, status = 200, headers: Record<string, string> = {}) => new Response(JSON.stringify(v), { status, headers });

describe("LiveRestClient request shapes", () => {
  it("POST /v1/orders: signed JSON body, parses { id, executions }", async () => {
    const { rest, calls } = client(() =>
      json({
        id: "ord-1",
        executions: [
          {
            id: "ex-1",
            type: "EXECUTION_TYPE_FILL",
            lastShares: "10",
            lastPx: { value: "0.43", currency: "USD" },
            commissionNotionalCollected: { value: "0.07", currency: "USD" },
            transactTime: "2026-10-20T14:00:01Z",
            order: { id: "ord-1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_FILLED", cumQuantity: 10, leavesQuantity: 0, avgPx: { value: "0.43", currency: "USD" } },
          },
        ],
      }),
    );
    const body = {
      marketSlug: "btc-1",
      intent: "ORDER_INTENT_BUY_LONG" as const,
      type: "ORDER_TYPE_LIMIT" as const,
      price: { value: "0.45", currency: "USD" as const },
      quantity: 10,
      tif: "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" as const,
      manualOrderIndicator: "MANUAL_ORDER_INDICATOR_MANUAL" as const,
      synchronousExecution: true,
    };
    const res = await rest.createOrder(body);
    expect(calls[0]).toMatchObject({ method: "POST", url: "https://api.polymarket.us/v1/orders", body });
    expect(calls[0]!.headers).toMatchObject({ "Content-Type": "application/json", "X-PM-Access-Key": "kid", "X-PM-Timestamp": String(NOW) });
    expect(calls[0]!.headers["X-PM-Signature"]).toMatch(/^[A-Za-z0-9+/]{86}==$/);
    expect(res.id).toBe("ord-1");
    expect(res.executions[0]).toMatchObject({
      id: "ex-1",
      type: "EXECUTION_TYPE_FILL",
      lastShares: 10,
      lastCents: 43,
      commissionUsd: 0.07,
      ts: Date.parse("2026-10-20T14:00:01Z"),
      order: { id: "ord-1", state: "ORDER_STATE_FILLED", cumQuantity: 10, avgCents: 43 },
    });
  });

  it("open orders, one order, cancel and cancel-all use the SDK paths", async () => {
    const { rest, calls } = client((c) => {
      if (c.path === "/v1/orders/open") return json({ orders: [{ id: "o1", marketSlug: "s", intent: "ORDER_INTENT_SELL_SHORT", state: "ORDER_STATE_NEW", price: { value: "0.6", currency: "USD" }, quantity: 5, cumQuantity: 0 }] });
      if (c.path === "/v1/order/o1") return json({ order: { id: "o1", state: "ORDER_STATE_CANCELED" } });
      if (c.path === "/v1/order/missing") return json({ message: "not found" }, 404);
      if (c.path === "/v1/order/o1/cancel") return new Response("", { status: 200 });
      if (c.path === "/v1/orders/open/cancel") return json({ canceledOrderIds: ["o1", "o2"] });
      return json({}, 500);
    });
    const open = await rest.openOrders(["s"]);
    expect(open[0]).toMatchObject({ id: "o1", intent: "ORDER_INTENT_SELL_SHORT", priceCents: 60, quantity: 5, cumQuantity: 0 });
    expect(calls[0]!.query.getAll("slugs")).toEqual(["s"]);
    await rest.openOrders();
    expect(calls[1]!.url).toBe("https://api.polymarket.us/v1/orders/open");

    expect((await rest.getOrder("o1"))?.state).toBe("ORDER_STATE_CANCELED");
    expect(await rest.getOrder("missing")).toBeNull();

    await rest.cancelOrder("o1", "s");
    expect(calls.at(-1)).toMatchObject({ method: "POST", path: "/v1/order/o1/cancel", body: { marketSlug: "s" } });

    expect(await rest.cancelAll(["s"])).toEqual(["o1", "o2"]);
    expect(calls.at(-1)).toMatchObject({ method: "POST", path: "/v1/orders/open/cancel", body: { slugs: ["s"] } });
    await rest.cancelAll();
    expect(calls.at(-1)!.body).toEqual({});
  });

  it("follows the positions cursor and prefers exact decimal quantities", async () => {
    const { rest, calls } = client((c) => {
      if (!c.query.get("cursor")) {
        return json({ positions: { "btc-a": { netPosition: "10", netPositionDecimal: "10.5", cost: { value: "4.20", currency: "USD" }, realized: { value: "0", currency: "USD" }, avgPx: { value: "0.40", currency: "USD" }, expired: false } }, nextCursor: "c2", eof: false });
      }
      return json({ positions: { "btc-b": { netPosition: "-4", cost: { value: "2.40", currency: "USD" }, realized: { value: "1.25", currency: "USD" }, expired: true } }, eof: true });
    });
    const ps = await rest.positions();
    expect(calls.map((c) => c.query.get("cursor"))).toEqual([null, "c2"]);
    expect(calls[0]!.path).toBe("/v1/portfolio/positions");
    expect(ps).toEqual({
      positions: [
        { slug: "btc-a", net: 10.5, avgCents: 40, avgHeldSide: false, costUsd: 4.2, realizedUsd: 0, expired: false },
        // no avgPx: derived from cost / |net|, the price paid for the held (short) instrument
        { slug: "btc-b", net: -4, avgCents: 60, avgHeldSide: true, costUsd: 2.4, realizedUsd: 1.25, expired: true },
      ],
      unparseable: [],
    });
  });

  it("skips a garbage netPosition (never a fabricated flat position) and reports its slug; market= filters", async () => {
    const { rest, calls } = client(() => json({ positions: { m: { netPosition: "garbage" }, ok: { netPosition: "2", avgPx: { value: "0.5" } } }, eof: true }));
    const r = await rest.positions({ market: "ok" });
    expect(r.unparseable).toEqual(["m"]);
    expect(r.positions.map((p) => p.slug)).toEqual(["ok"]);
    expect(calls[0]!.query.get("market")).toBe("ok");
  });

  it("reads the USD balance", async () => {
    const { rest, calls } = client(() => json({ balances: [{ currentBalance: 1234.5, buyingPower: 1000, currency: "USD" }] }));
    expect(await rest.balance()).toEqual({ currency: "USD", balanceUsd: 1234.5, buyingPowerUsd: 1000 });
    expect(calls[0]).toMatchObject({ method: "GET", path: "/v1/account/balances" });
  });

  it("maps errors: 4xx definite, 5xx / timeout / network ambiguous, message from the body", async () => {
    const bad = client(() => json({ message: "insufficient buying power" }, 400)).rest;
    const e1 = (await bad.balance().catch((e) => e)) as LiveHttpError;
    expect(e1).toBeInstanceOf(LiveHttpError);
    expect(e1.status).toBe(400);
    expect(e1.ambiguous).toBe(false);
    expect(e1.message).toContain("insufficient buying power");

    const down = client(() => json({ error: "upstream" }, 503, { "retry-after": "3" })).rest;
    const e2 = (await down.balance().catch((e) => e)) as LiveHttpError;
    expect(e2.ambiguous).toBe(true);
    expect(e2.retryAfterMs).toBe(3000);

    const net = client(() => {
      throw new TypeError("fetch failed");
    }).rest;
    const e3 = (await net.balance().catch((e) => e)) as LiveHttpError;
    expect(e3.status).toBe(0);
    expect(e3.ambiguous).toBe(true);
  });

  it("a 2xx with an unusable body is ambiguous (the order was accepted), never a definite rejection", async () => {
    const noId = client(() => json({ order: { orderId: 5 } })).rest;
    const e1 = (await noId.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" }).catch((e) => e)) as LiveHttpError;
    expect(e1).toBeInstanceOf(LiveHttpError);
    expect(e1.status).toBe(200);
    expect(e1.ambiguous).toBe(true);

    const badJson = client(() => new Response("{not json", { status: 200 })).rest;
    const e2 = (await badJson.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" }).catch((e) => e)) as LiveHttpError;
    expect(e2).toBeInstanceOf(LiveHttpError);
    expect(e2.ambiguous).toBe(true);

    // tolerated id shapes
    expect((await client(() => json({ orderId: "a1" })).rest.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" })).id).toBe("a1");
    expect((await client(() => json({ order: { id: "a2" } })).rest.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" })).id).toBe("a2");
  });

  it("a stalled or reset response body maps to an ambiguous LiveHttpError (not a raw AbortError / TypeError)", async () => {
    const reset = client(() => ({ ok: true, status: 200, statusText: "OK", headers: new Headers(), text: () => Promise.reject(new TypeError("terminated")) }) as unknown as Response).rest;
    const e1 = (await reset.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" }).catch((e) => e)) as LiveHttpError;
    expect(e1).toBeInstanceOf(LiveHttpError);
    expect(e1.status).toBe(0);
    expect(e1.ambiguous).toBe(true);
    expect(e1.message).toMatch(/response body.*terminated/);

    const abort = Object.assign(new Error("The operation was aborted"), { name: "AbortError" });
    const stalled = client(() => ({ ok: true, status: 200, statusText: "OK", headers: new Headers(), text: () => Promise.reject(abort) }) as unknown as Response).rest;
    const e2 = (await stalled.createOrder({ marketSlug: "s", intent: "ORDER_INTENT_BUY_LONG", type: "ORDER_TYPE_LIMIT" }).catch((e) => e)) as LiveHttpError;
    expect(e2).toBeInstanceOf(LiveHttpError);
    expect(e2.status).toBe(408);
    expect(e2.ambiguous).toBe(true);
  });

  it("corrects the signing clock from the server Date header when skewed", async () => {
    const serverNow = NOW + 45_000;
    const { rest, calls } = client(() => json({ balances: [] }, 200, { date: new Date(serverNow).toUTCString() }));
    await rest.balance();
    expect(Math.abs(rest.clockOffsetMs - 45_000)).toBeLessThan(1500);
    await rest.balance();
    expect(Math.abs(Number(calls[1]!.headers["X-PM-Timestamp"]) - serverNow)).toBeLessThan(1500);
  });
});

describe("live wire helpers", () => {
  it("formats cents as an Amount dollar string", () => {
    expect(centsToDollars(43)).toBe("0.43");
    expect(centsToDollars(50)).toBe("0.50");
    expect(centsToDollars(4.5)).toBe("0.045");
    expect(centsToDollars(99)).toBe("0.99");
  });

  it("keeps an order's avgPx unrounded (implied delta-fill prices)", () => {
    expect(exactCents({ value: "0.44437" })).toBeCloseTo(44.437, 9);
    expect(parseOrder({ id: "o", avgPx: { value: "0.44437" } })!.avgCents).toBeCloseTo(44.437, 9);
    expect(exactCents("garbage")).toBeNull();
  });

  it("maps every ORDER_STATE_*", () => {
    expect(mapOrderState("ORDER_STATE_NEW")).toBe("open");
    expect(mapOrderState("ORDER_STATE_PENDING_NEW")).toBe("pending");
    expect(mapOrderState("ORDER_STATE_PARTIALLY_FILLED")).toBe("partial");
    expect(mapOrderState("ORDER_STATE_FILLED")).toBe("filled");
    expect(mapOrderState("ORDER_STATE_CANCELED")).toBe("cancelled");
    expect(mapOrderState("ORDER_STATE_EXPIRED")).toBe("cancelled");
    expect(mapOrderState("ORDER_STATE_REJECTED")).toBe("rejected");
    expect(mapOrderState("nope")).toBeNull();
  });

  it("classifies private frames (current and legacy envelopes)", () => {
    expect(parsePrivateFrame('{"heartbeat":{}}').kind).toBe("heartbeat");
    expect(parsePrivateFrame('{"requestId":"r","error":"deadline exceeded"}')).toEqual({ kind: "error", error: "deadline exceeded", requestId: "r" });
    const snap = parsePrivateFrame(JSON.stringify({ requestId: "s", subscriptionType: "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT", orderSubscriptionSnapshot: { orders: [{ id: "o1", state: "ORDER_STATE_NEW" }], eof: true } }));
    expect(snap).toMatchObject({ kind: "orderSnapshot", eof: true, orders: [{ id: "o1" }] });
    const upd = parsePrivateFrame(JSON.stringify({ orderSubscriptionUpdate: { execution: { id: "e1", type: "EXECUTION_TYPE_PARTIAL_FILL", order: { id: "o1", cumQuantity: 3 } } } }));
    expect(upd).toMatchObject({ kind: "execution", execution: { id: "e1", order: { id: "o1", cumQuantity: 3 } } });
    const pos = parsePrivateFrame(JSON.stringify({ positionSubscription: { beforePosition: null, afterPosition: { netPosition: "10", netPositionDecimal: "10.25", marketMetadata: { slug: "m1" }, realized: { value: "0" } } } }));
    expect(pos).toMatchObject({ kind: "position", slug: "m1", after: { net: 10.25 } });
    const bal = parsePrivateFrame(JSON.stringify({ accountBalancesUpdate: { balanceChange: { afterBalance: { currentBalance: 50, buyingPower: 40, currency: "USD" } } } }));
    expect(bal).toEqual({ kind: "balance", balance: { currency: "USD", balanceUsd: 50, buyingPowerUsd: 40 } });
    const balSnap = parsePrivateFrame(JSON.stringify({ accountBalancesSnapshot: { balances: [{ currentBalance: 7, buyingPower: 6, currency: "USD" }] } }));
    expect(balSnap).toMatchObject({ kind: "balance", balance: { balanceUsd: 7 } });
    expect(parsePrivateFrame(JSON.stringify({ accountBalanceSubscriptionUpdate: { balance: 9, buyingPower: 8 } }))).toMatchObject({ kind: "balance", balance: { balanceUsd: 9, buyingPowerUsd: 8 } });
    expect(parsePrivateFrame("not json").kind).toBe("other");
  });
});
