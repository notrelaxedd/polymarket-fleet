// LiveExecutor: Order -> POST /v1/orders mapping, fills from the response / private socket / polls (deduped by the
// cumulative filled quantity), in-flight merging, ambiguous submits, cancel. Offline (injected fetch).
import { afterEach, describe, expect, it } from "vitest";
import type { MarketWindow, Order } from "../../shared/protocol.ts";
import type { LiveConfig } from "../src/live/config.ts";
import { LiveExecutor } from "../src/live/executor.ts";
import { LiveRestClient } from "../src/live/rest.ts";
import { parseExecution, parseOrder } from "../src/live/wire.ts";
import type { Logger } from "../src/log.ts";
import type { ExecContext, ExternalOrderUpdate } from "../src/modules/types.ts";
import { ApiError } from "../src/server/errors.ts";

const quiet = { info() {}, warn() {}, error() {}, debug() {}, child() { return quiet; } } as unknown as Logger;
const SECRET = Buffer.alloc(32, 3).toString("base64");
const NOW = Date.parse("2026-10-20T14:05:00Z");

const win = (up = "btc-1:long", down = "btc-1:short"): MarketWindow => ({
  id: "btc-1",
  title: "BTC Up or Down",
  startTs: NOW - 5 * 60_000,
  endTs: NOW + 10 * 60_000,
  priceToBeat: 67000,
  upInstrument: up,
  downInstrument: down,
  status: "live",
  result: null,
  settlePrice: null,
});

const ctx = (w = win(), fee: number | null = null): ExecContext => ({ window: w, book: null, odds: null, feeCentsPerContract: fee });

function order(p: Partial<Order> = {}): Order {
  return {
    id: "loc-1",
    clientOrderId: null,
    externalId: null,
    mode: "live",
    windowId: "btc-1",
    instrument: "btc-1:long",
    side: "up",
    action: "buy",
    type: "marketable",
    limitCents: 45,
    maxSlippageCents: 2,
    qty: 10.7,
    notionalUsd: 4.5,
    filledQty: 0,
    avgFillCents: null,
    feesUsd: 0,
    status: "pending",
    reason: "manual",
    bracketId: null,
    positionId: null,
    error: null,
    createdTs: NOW,
    updatedTs: NOW,
    ...p,
  };
}

interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  body: any;
}

type Handler = (c: Call) => Response | Promise<Response>;
const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
const amt = (d: string) => ({ value: d, currency: "USD" });

const executors: LiveExecutor[] = [];
afterEach(() => {
  for (const e of executors.splice(0)) e.stop();
});

function setup(handler: Handler, cfg: Partial<LiveConfig> = {}) {
  const calls: Call[] = [];
  let h = handler;
  let t = NOW;
  let local: { qty: number; costUsd: number } | null = null;
  const notices: string[] = [];
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const c = { method: String(init.method), path: u.pathname, query: u.searchParams, body: init.body ? JSON.parse(String(init.body)) : undefined };
    calls.push(c);
    return h(c);
  }) as unknown as typeof fetch;
  const rest = new LiveRestClient({ baseUrl: "https://api.polymarket.us", keyId: "kid", secretKey: SECRET, fetchImpl, now: () => t });
  const emitted: ExternalOrderUpdate[] = [];
  const reconciles: string[] = [];
  let connected = true;
  const ex = new LiveExecutor({
    rest,
    cfg: { shortPriceQuote: "short", qtyStep: 1, tickCents: 1, ...cfg },
    logger: quiet,
    now: () => t,
    emit: (u) => emitted.push(u),
    requestReconcile: (r) => reconciles.push(r),
    streamConnected: () => connected,
    localPosition: () => local,
    notify: (_level, title) => notices.push(title),
    resolve: { scheduleMs: [] }, // tests drive the resolution with resolveUnconfirmed()
  });
  executors.push(ex);
  return {
    ex,
    calls,
    emitted,
    reconciles,
    notices,
    setHandler: (n: Handler) => (h = n),
    setConnected: (v: boolean) => (connected = v),
    advance: (ms: number) => (t += ms),
    setLocal: (v: { qty: number; costUsd: number } | null) => (local = v),
  };
}

const fillExec = (id: string, orderId: string, cum: number, px: string, last: number, state = "ORDER_STATE_FILLED", fee?: string) => ({
  id,
  type: state === "ORDER_STATE_FILLED" ? "EXECUTION_TYPE_FILL" : "EXECUTION_TYPE_PARTIAL_FILL",
  lastShares: String(last),
  lastPx: amt(px),
  ...(fee ? { commissionNotionalCollected: amt(fee) } : {}),
  transactTime: "2026-10-20T14:05:01Z",
  order: { id: orderId, marketSlug: "btc-1", state, cumQuantity: cum, avgPx: amt(px) },
});

describe("LiveExecutor.submit", () => {
  it("maps a marketable Up buy to a limit IOC BUY_LONG and returns the synchronous fills", async () => {
    const { ex, calls } = setup(() => json({ id: "x-1", executions: [fillExec("e1", "x-1", 10, "0.44", 10, "ORDER_STATE_FILLED", "0.0700")] }));
    const r = await ex.submit(order(), ctx());
    expect(calls).toHaveLength(1);
    expect(calls[0]!.method).toBe("POST");
    expect(calls[0]!.path).toBe("/v1/orders");
    expect(calls[0]!.body).toEqual({
      marketSlug: "btc-1",
      intent: "ORDER_INTENT_BUY_LONG",
      type: "ORDER_TYPE_LIMIT",
      price: { value: "0.45", currency: "USD" },
      quantity: 10, // 10.7 rounded down to whole contracts
      tif: "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
      manualOrderIndicator: "MANUAL_ORDER_INDICATOR_MANUAL",
      synchronousExecution: true,
    });
    expect(r).toEqual({
      externalId: "x-1",
      status: "filled",
      error: null,
      fills: [{ externalId: "e1", priceCents: 44, qty: 10, feeUsd: 0.07, ts: Date.parse("2026-10-20T14:05:01Z") }],
    });
  });

  it("limit sells rest GTC; bracket exits are flagged AUTOMATIC; prices tick away from the market", async () => {
    const { ex, calls } = setup(() => json({ id: "x-2" }));
    const r = await ex.submit(order({ action: "sell", type: "limit", limitCents: 61.4, qty: 5, notionalUsd: null, reason: "tp" }), ctx());
    expect(calls[0]!.body).toMatchObject({
      intent: "ORDER_INTENT_SELL_LONG",
      tif: "TIME_IN_FORCE_GOOD_TILL_CANCEL",
      price: { value: "0.62", currency: "USD" },
      quantity: 5,
      manualOrderIndicator: "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    });
    expect(r).toMatchObject({ externalId: "x-2", status: "open", fills: [] });
  });

  it("Down on the short instrument: BUY_SHORT, price quoted per POLY_SHORT_PRICE_QUOTE", async () => {
    const own = setup(() => json({ id: "s-1", executions: [fillExec("e1", "s-1", 4, "0.40", 4)] }));
    const o = order({ instrument: "btc-1:short", side: "down", limitCents: 41, qty: 4 });
    const r1 = await own.ex.submit(o, ctx());
    expect(own.calls[0]!.body).toMatchObject({ intent: "ORDER_INTENT_BUY_SHORT", price: { value: "0.41" } });
    expect(r1.fills[0]!.priceCents).toBe(40);

    const longQuoted = setup(() => json({ id: "s-2", executions: [fillExec("e1", "s-2", 4, "0.60", 4)] }), { shortPriceQuote: "long" });
    const r2 = await longQuoted.ex.submit(o, ctx());
    expect(longQuoted.calls[0]!.body).toMatchObject({ intent: "ORDER_INTENT_BUY_SHORT", price: { value: "0.59" } });
    expect(r2.fills[0]!.priceCents).toBe(40);
  });

  it("Up as the short side of a market (flipped window) trades the short intent", async () => {
    const { ex, calls } = setup(() => json({ id: "f-1" }));
    await ex.submit(order({ instrument: "btc-1:short", type: "limit" }), ctx(win("btc-1:short", "btc-1:long")));
    expect(calls[0]!.body.intent).toBe("ORDER_INTENT_BUY_SHORT");
  });

  it("rejects locally (no HTTP) for a foreign instrument, an ended window, no price or a zero quantity", async () => {
    const { ex, calls } = setup(() => json({ id: "never" }));
    expect((await ex.submit(order({ instrument: "other:long" }), ctx())).status).toBe("rejected");
    expect((await ex.submit(order(), ctx({ ...win(), endTs: NOW - 1 }))).error).toMatch(/ended/);
    expect((await ex.submit(order({ limitCents: null }), ctx())).error).toMatch(/limit price/);
    expect((await ex.submit(order({ qty: 0.6 }), ctx())).error).toMatch(/rounds to 0/);
    expect(calls).toHaveLength(0);
  });

  it("falls back to the USD notional when the order carries no quantity", async () => {
    const { ex, calls } = setup(() => json({ id: "n-1" }));
    await ex.submit(order({ qty: 0, notionalUsd: 9, limitCents: 45 }), ctx());
    expect(calls[0]!.body.quantity).toBe(20);
  });

  it("returns an exchange 4xx as a rejected result with the exchange's message", async () => {
    const { ex, reconciles } = setup(() => json({ message: "insufficient buying power" }, 400));
    const r = await ex.submit(order(), ctx());
    expect(r).toMatchObject({ status: "rejected", externalId: null });
    expect(r.error).toContain("insufficient buying power");
    expect(reconciles).toHaveLength(0);
  });

  it("after an ambiguous failure adopts the matching open order instead of re-sending", async () => {
    const { ex, calls } = setup((c) => {
      if (c.path === "/v1/orders") return json({ error: "gateway timeout" }, 504);
      if (c.path === "/v1/orders/open") {
        return json({ orders: [{ id: "adopt-1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_NEW", price: amt("0.45"), quantity: 10, cumQuantity: 0, createTime: new Date(NOW).toISOString() }] });
      }
      return json({}, 404);
    });
    const r = await ex.submit(order({ type: "limit" }), ctx());
    expect(calls.filter((c) => c.path === "/v1/orders")).toHaveLength(1); // never retried
    expect(calls[1]!.query.getAll("slugs")).toEqual(["btc-1"]);
    expect(r).toMatchObject({ externalId: "adopt-1", status: "open", error: null });
  });

  it("an ambiguous failure is never 'rejected': the order stays pending under an alias while it is confirmed", async () => {
    const { ex, calls } = setup((c) => (c.path === "/v1/orders" ? json({}, 503) : c.path === "/v1/orders/open" ? json({ orders: [] }) : json({ positions: {}, eof: true })));
    const r = await ex.submit(order(), ctx());
    expect(r).toMatchObject({ status: "pending", externalId: "unconfirmed:loc-1", fills: [] });
    expect(r.error).toMatch(/did not confirm.*confirming its outcome/);
    expect(calls.filter((c) => c.path === "/v1/orders")).toHaveLength(1);
    expect(ex.busy()).toBe(true); // position reconcile waits for the outcome
    expect(ex.openOrderCount()).toBe(1);
    // the same instrument + action is refused until then (no accidental doubling); the other side is not
    const again = await ex.submit(order({ id: "loc-2" }), ctx());
    expect(again).toMatchObject({ status: "rejected" });
    expect(again.error).toMatch(/still being confirmed/);
    expect(calls.filter((c) => c.path === "/v1/orders")).toHaveLength(1);
    await ex.submit(order({ id: "loc-3", instrument: "btc-1:short", side: "down" }), ctx());
    expect(calls.filter((c) => c.path === "/v1/orders")).toHaveLength(2);
  });

  it("a timed-out IOC that filled is booked from the positions delta (two equal reads), at the cost-derived price", async () => {
    let positions: Record<string, unknown> = {};
    const { ex, calls, emitted, advance, setLocal, notices } = setup((c) => {
      if (c.path === "/v1/orders") throw Object.assign(new Error("aborted"), { name: "TimeoutError" });
      if (c.path === "/v1/orders/open") return json({ orders: [] }); // an IOC never rests
      if (c.path === "/v1/portfolio/positions") return json({ positions, eof: true });
      return json({}, 404);
    });
    setLocal({ qty: 5, costUsd: 2.0 }); // already held 5 @ 40c
    const r = await ex.submit(order(), ctx());
    expect(r).toMatchObject({ status: "pending", externalId: "unconfirmed:loc-1" });
    expect(calls.map((c) => c.path)).toEqual(["/v1/orders"]); // after a timeout: no inline sweep doubling the wait

    positions = { "btc-1": { netPosition: "15", cost: amt("6.40") } }; // +10 for $4.40 -> 44c
    advance(1000);
    await ex.resolveUnconfirmed();
    expect(emitted).toHaveLength(0); // one read is not enough (the endpoint may lag)
    expect(calls.find((c) => c.path === "/v1/portfolio/positions")!.query.get("market")).toBe("btc-1");
    advance(2500);
    await ex.resolveUnconfirmed();
    expect(emitted).toEqual([
      { externalId: "unconfirmed:loc-1", status: "filled", error: null, fill: { externalId: "unconfirmed:loc-1:positions", priceCents: 44, qty: 10, feeUsd: null, ts: NOW + 3500 } },
    ]);
    expect(notices).toEqual(["Live order confirmed from positions"]);
    expect(ex.openOrderCount()).toBe(0);
    expect(ex.lastFillTs("btc-1")).toBe(NOW + 3500);
    advance(3000);
    expect(ex.busy()).toBe(false);
  });

  it("an unconfirmed IOC with a confirmed zero delta is cancelled; an unreachable exchange gives up after the budget", async () => {
    const a = setup((c) => (c.path === "/v1/orders" ? json({}, 502) : c.path === "/v1/orders/open" ? json({ orders: [] }) : json({ positions: {}, eof: true })));
    await a.ex.submit(order(), ctx());
    a.advance(3000);
    await a.ex.resolveUnconfirmed();
    a.advance(3000);
    await a.ex.resolveUnconfirmed(); // equal reads but too early to call it nothing
    expect(a.emitted).toHaveLength(0);
    a.advance(3000);
    await a.ex.resolveUnconfirmed();
    expect(a.emitted).toEqual([{ externalId: "unconfirmed:loc-1", status: "cancelled", error: expect.stringMatching(/nothing filled/) }]);

    const b = setup(() => {
      throw new TypeError("fetch failed");
    });
    const r = await b.ex.submit(order(), ctx());
    expect(r.status).toBe("pending");
    b.advance(10_000);
    await b.ex.resolveUnconfirmed();
    expect(b.emitted).toHaveLength(0);
    b.advance(21_000);
    await b.ex.resolveUnconfirmed();
    expect(b.emitted).toEqual([{ externalId: "unconfirmed:loc-1", status: "cancelled", error: expect.stringMatching(/outcome unknown/) }]);
    expect(b.notices).toEqual(["Live order outcome unknown"]);
    expect(b.reconciles).toEqual(["unconfirmed order outcome unknown"]);
    expect(b.ex.busy()).toBe(false);
  });

  it("links an unconfirmed GTC to the order the socket showed meanwhile; later updates and cancel use it", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const { ex, calls, emitted } = setup(async (c) => {
      if (c.path === "/v1/orders") {
        await gate;
        throw new TypeError("socket hang up");
      }
      if (c.path === "/v1/order/G1") return json({ order: { id: "G1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_PARTIALLY_FILLED", price: amt("0.45"), quantity: 10, cumQuantity: 4, avgPx: amt("0.45") } });
      if (c.path === "/v1/order/G1/cancel") return json({});
      return json({}, 500);
    });
    const p = ex.submit(order({ type: "limit", qty: 10 }), ctx());
    await new Promise((r) => setTimeout(r, 5));
    // while the POST hangs, the socket reports the new order and a partial fill
    ex.onExecution(parseExecution({ id: "n1", type: "EXECUTION_TYPE_NEW", order: { id: "G1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_NEW", price: amt("0.45"), quantity: 10, cumQuantity: 0 } })!);
    ex.onExecution(parseExecution({ ...fillExec("f1", "G1", 4, "0.45", 4, "ORDER_STATE_PARTIALLY_FILLED"), order: { id: "G1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_PARTIALLY_FILLED", price: amt("0.45"), quantity: 10, cumQuantity: 4, avgPx: amt("0.45") } })!);
    release();
    const r = await p;
    expect(r).toMatchObject({ status: "pending", externalId: "unconfirmed:loc-1" });
    expect(emitted).toHaveLength(0);

    await ex.resolveUnconfirmed();
    expect(emitted).toEqual([{ externalId: "unconfirmed:loc-1", status: "partial", error: null, fill: { externalId: "G1:4", priceCents: 45, qty: 4, feeUsd: null, ts: NOW } }]);
    ex.onExecution(parseExecution(fillExec("f2", "G1", 10, "0.45", 6))!);
    expect(emitted[1]).toMatchObject({ externalId: "unconfirmed:loc-1", status: "filled", fill: { externalId: "f2", qty: 6 } });

    await ex.cancel(order({ externalId: "unconfirmed:loc-1" }));
    expect(calls.at(-1)).toMatchObject({ path: "/v1/order/G1/cancel" });
  });

  it("adopt() honours the REST clock offset and finds an order already tracked from the socket", async () => {
    const { ex } = setup((c) => {
      if (c.path === "/v1/orders") return json({}, 504);
      // the exchange clock runs 60 s ahead: its createTime is "in the future" for us, but recent for it
      if (c.path === "/v1/orders/open") return json({ orders: [{ id: "Z1", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_NEW", price: amt("0.45"), quantity: 10, cumQuantity: 0, createTime: new Date(NOW + 60_000).toISOString() }] });
      return json({}, 404);
    });
    (ex as unknown as { d: { rest: { clockOffsetMs: number } } }).d.rest.clockOffsetMs = 60_000;
    const r = await ex.submit(order({ type: "limit" }), ctx());
    expect(r).toMatchObject({ externalId: "Z1", status: "open" });

    const late = setup((c) => {
      if (c.path === "/v1/orders") return json({}, 504);
      // an order created long before this submit (another client's identical order) is never adopted
      if (c.path === "/v1/orders/open") return json({ orders: [{ id: "OLD", marketSlug: "btc-1", intent: "ORDER_INTENT_BUY_LONG", state: "ORDER_STATE_NEW", price: amt("0.45"), quantity: 10, cumQuantity: 0, createTime: new Date(NOW - 120_000).toISOString() }] });
      return json({}, 404);
    });
    expect((await late.ex.submit(order({ type: "limit" }), ctx())).status).toBe("pending");
  });

  it("uses the settings fee override when the exchange reports no commission, else [FEE] (null)", async () => {
    const a = setup(() => json({ id: "a", executions: [fillExec("e1", "a", 10, "0.44", 10)] }));
    expect((await a.ex.submit(order(), ctx(win(), 1.5))).fills[0]!.feeUsd).toBe(0.15);
    const b = setup(() => json({ id: "b", executions: [fillExec("e1", "b", 10, "0.44", 10)] }));
    expect((await b.ex.submit(order(), ctx(win(), null))).fills[0]!.feeUsd).toBeNull();
  });
});

describe("LiveExecutor async updates", () => {
  it("applies private-socket executions once (dedupe by execution id and cumulative quantity)", async () => {
    const { ex, emitted, setHandler } = setup(() => json({ id: "L1" }));
    const r = await ex.submit(order({ type: "limit", qty: 10 }), ctx());
    expect(r.status).toBe("open");

    const e1 = parseExecution(fillExec("e1", "L1", 4, "0.45", 4, "ORDER_STATE_PARTIALLY_FILLED"))!;
    ex.onExecution(e1);
    ex.onExecution(e1); // duplicate frame
    expect(emitted).toEqual([{ externalId: "L1", status: "partial", error: null, fill: { externalId: "e1", priceCents: 45, qty: 4, feeUsd: null, ts: Date.parse("2026-10-20T14:05:01Z") } }]);

    // a poll that reports the same cumulative quantity adds nothing; a later one with more adds only the delta
    setHandler(() => json({ order: { id: "L1", state: "ORDER_STATE_PARTIALLY_FILLED", cumQuantity: 4, avgPx: amt("0.45") } }));
    await ex.pollOpen(true);
    expect(emitted).toHaveLength(1);
    setHandler(() => json({ order: { id: "L1", state: "ORDER_STATE_FILLED", cumQuantity: 10, avgPx: amt("0.444"), commissionNotionalTotalCollected: amt("0.10") } }));
    await ex.pollOpen(true);
    expect(emitted).toHaveLength(2);
    // implied price of the missing 6 contracts: (10 * 44.4 - 4 * 45) / 6 = 44
    expect(emitted[1]).toMatchObject({ externalId: "L1", status: "filled", fill: { externalId: "L1:10", qty: 6, priceCents: 44, feeUsd: 0.1 } });

    // the socket's copy of that execution arrives late: nothing new
    ex.onExecution(parseExecution(fillExec("e2", "L1", 10, "0.44", 6))!);
    expect(emitted).toHaveLength(2);
  });

  it("reports cancels and rejects as status-only updates", async () => {
    const { ex, emitted } = setup(() => json({ id: "C1" }));
    await ex.submit(order({ type: "limit" }), ctx());
    ex.onExecution(parseExecution({ id: "e9", type: "EXECUTION_TYPE_CANCELED", order: { id: "C1", state: "ORDER_STATE_CANCELED", cumQuantity: 0 } })!);
    expect(emitted).toEqual([{ externalId: "C1", status: "cancelled", error: null }]);
    // an out-of-order NEW after the cancel does not resurrect it
    ex.onExecution(parseExecution({ id: "e0", type: "EXECUTION_TYPE_NEW", order: { id: "C1", state: "ORDER_STATE_NEW", cumQuantity: 0 } })!);
    expect(emitted).toHaveLength(1);
  });

  it("merges socket executions that beat the POST response into that submit (not emitted twice)", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const { ex, emitted } = setup(async () => {
      await gate;
      return json({ id: "R1", executions: [fillExec("e1", "R1", 10, "0.44", 10)] });
    });
    const p = ex.submit(order(), ctx());
    await new Promise((r) => setTimeout(r, 5));
    ex.onExecution(parseExecution(fillExec("e1", "R1", 10, "0.44", 10))!); // arrives first over the socket
    release();
    const r = await p;
    expect(r.fills).toHaveLength(1);
    expect(r.fills[0]).toMatchObject({ externalId: "e1", qty: 10, priceCents: 44 });
    expect(r.status).toBe("filled");
    expect(emitted).toHaveLength(0);
  });

  it("ignores orders it did not place and does not re-emit fills seeded from trading after a restart", async () => {
    const { ex, emitted } = setup(() => json({}));
    ex.onExecution(parseExecution(fillExec("z1", "foreign", 3, "0.5", 3))!);
    expect(emitted).toHaveLength(0);

    ex.seed([order({ externalId: "S1", status: "partial", filledQty: 4, avgFillCents: 45, type: "limit" })]);
    ex.onExecution(parseExecution(fillExec("s-e2", "S1", 6, "0.45", 2, "ORDER_STATE_PARTIALLY_FILLED"))!);
    expect(emitted).toEqual([expect.objectContaining({ externalId: "S1", fill: expect.objectContaining({ qty: 2, priceCents: 45 }) })]);
  });

  it("order snapshot: polls our open orders the snapshot no longer lists", async () => {
    const { ex, calls, setHandler, emitted } = setup(() => json({ id: "P1" }));
    await ex.submit(order({ type: "limit" }), ctx());
    setHandler(() => json({ order: { id: "P1", state: "ORDER_STATE_CANCELED", cumQuantity: 0 } }));
    ex.onOrderSnapshot([], false);
    ex.onOrderSnapshot([parseOrder({ id: "other", state: "ORDER_STATE_NEW" })!], true);
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.some((c) => c.path === "/v1/order/P1")).toBe(true);
    expect(emitted).toEqual([{ externalId: "P1", status: "cancelled", error: null }]);
  });
});

describe("LiveExecutor fill prices and vanished orders", () => {
  it("an implied delta price outside the order's limit is not used (the limit is); no price at all books nothing", async () => {
    const { ex, emitted, setHandler } = setup(() => json({ id: "I1" }));
    await ex.submit(order({ type: "limit", qty: 10 }), ctx());
    ex.onExecution(parseExecution(fillExec("e1", "I1", 4, "0.45", 4, "ORDER_STATE_PARTIALLY_FILLED"))!);
    // a bogus avgPx (would imply (10 * 60 - 4 * 45) / 6 = 70c on a 45c buy)
    setHandler(() => json({ order: { id: "I1", state: "ORDER_STATE_FILLED", cumQuantity: 10, avgPx: amt("0.60") } }));
    await ex.pollOpen(true);
    expect(emitted[1]).toMatchObject({ externalId: "I1", status: "filled", fill: { qty: 6, priceCents: 45 } });

    // a seeded order without a limit and a poll without any price: nothing is booked (never at 0), state waits
    const b = setup(() => json({ order: { id: "S9", state: "ORDER_STATE_FILLED", cumQuantity: 3 } }));
    b.ex.seed([order({ externalId: "S9", status: "open", limitCents: null, type: "limit" })]);
    await b.ex.pollOpen(true);
    expect(b.emitted).toEqual([]);
    expect(b.ex.openOrderCount()).toBe(1);
    b.setHandler(() => json({ order: { id: "S9", state: "ORDER_STATE_FILLED", cumQuantity: 3, avgPx: amt("0.52") } }));
    await b.ex.pollOpen(true);
    expect(b.emitted).toEqual([expect.objectContaining({ externalId: "S9", status: "filled", fill: expect.objectContaining({ qty: 3, priceCents: 52 }) })]);
  });

  it("an IOC that is no longer queryable (404 twice) is reported cancelled instead of pending forever", async () => {
    const { ex, emitted, setHandler } = setup(() => json({ id: "V1" }));
    await ex.submit(order(), ctx()); // IOC, no executions in the response: pending
    setHandler(() => json({ message: "not found" }, 404));
    await ex.pollOpen(true);
    expect(emitted).toHaveLength(0);
    await ex.pollOpen(true);
    expect(emitted).toEqual([{ externalId: "V1", status: "cancelled", error: "order no longer on the exchange" }]);
    expect(ex.openOrderCount()).toBe(0);
  });

  it("drain() cancels our resting orders one by one and reports their final state", async () => {
    const { ex, calls, emitted, setHandler } = setup(() => json({ id: "D1" }));
    await ex.submit(order({ type: "limit" }), ctx());
    setHandler((c) => (c.path === "/v1/order/D1/cancel" ? json({}) : json({ order: { id: "D1", state: "ORDER_STATE_CANCELED", cumQuantity: 0 } })));
    const r = await ex.drain();
    expect(r).toEqual({ cancelled: 1, failed: [], unknown: 0 });
    expect(calls.some((c) => c.path === "/v1/orders/open/cancel")).toBe(false); // other clients' orders untouched
    expect(emitted).toEqual([{ externalId: "D1", status: "cancelled", error: null }]);
  });
});

describe("LiveExecutor.cancel", () => {
  it("a cancel 404 polls the order first, so a fill it missed reaches trading before success", async () => {
    const { ex, emitted, reconciles, setHandler } = setup(() => json({ id: "M1" }));
    await ex.submit(order({ type: "limit", qty: 10 }), ctx());
    setHandler((c) => (c.path.endsWith("/cancel") ? json({ message: "not found" }, 404) : json({ order: { id: "M1", state: "ORDER_STATE_FILLED", cumQuantity: 10, avgPx: amt("0.44") } })));
    await ex.cancel(order({ externalId: "M1", type: "limit" }));
    expect(emitted).toEqual([expect.objectContaining({ externalId: "M1", status: "filled", fill: expect.objectContaining({ qty: 10, priceCents: 44 }) })]);
    expect(reconciles).toHaveLength(0);

    // unknown to the exchange as well: success, with a reconcile
    const b = setup(() => json({ id: "M2" }));
    await b.ex.submit(order({ type: "limit" }), ctx());
    b.setHandler(() => json({ message: "not found" }, 404));
    await b.ex.cancel(order({ externalId: "M2" }));
    expect(b.reconciles).toEqual(["cancel 404: order state unknown"]);
    expect(b.ex.openOrderCount()).toBe(0);
  });

  it("POSTs /v1/order/{id}/cancel with the market slug; 404 is already gone; other errors -> 502 exchange_error", async () => {
    let status = 200;
    const { ex, calls } = setup((c) => (c.path.endsWith("/cancel") ? new Response("{}", { status }) : json({ order: { id: "K1", state: "ORDER_STATE_CANCELED" } })));
    await ex.cancel(order({ externalId: "K1", instrument: "btc-1:short" }));
    expect(calls[0]).toMatchObject({ method: "POST", path: "/v1/order/K1/cancel", body: { marketSlug: "btc-1" } });
    status = 404;
    await expect(ex.cancel(order({ externalId: "K1" }))).resolves.toBeUndefined();
    status = 500;
    const err = await ex.cancel(order({ externalId: "K1" })).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ statusCode: 502, code: "exchange_error" });
    await ex.cancel(order({ externalId: null })); // nothing on the exchange yet
    expect(calls.filter((c) => c.path.endsWith("/cancel"))).toHaveLength(3);
  });
});
