// Mock paper trading (lib/mock/trading.ts) driven through its routes with a fake MockCtx: book-walk fills,
// resting limits, OCO bracket exits, settlement at rollover, risk rejection, live-session gate, kill helpers.
import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import type { BookLevels, MarketWindow, OddsSnap, PlaceOrderResponse, ServerMsg, Settings, Bracket, Order } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { checkOrder, computeRiskState } from "@shared/risk";
import { ApiError } from "../../../lib/api-error";
import { createMockTrading } from "../../../lib/mock/trading";
import type { MockCtx, MockRequest, MockTrading } from "../../../lib/mock/types";

const T0 = 1_760_000_400_000;
const win: MarketWindow = {
  id: "w1",
  title: "BTC",
  startTs: T0,
  endTs: T0 + 900_000,
  priceToBeat: 67000,
  upInstrument: "w1:up",
  downInstrument: "w1:down",
  status: "live",
  result: null,
  settlePrice: null,
};

let now = T0 + 60_000;
let book: BookLevels;
let btcPx = 67050;
let settings: Settings;
let emitted: ServerMsg[];
let confirmed = false;
let t: MockTrading;

const mkBook = (bid: number, ask: number, size = 100): BookLevels => ({
  ts: now,
  windowId: "w1",
  instrument: "w1:up",
  bids: [0, 1, 2].map((i) => ({ price: bid - i, size })),
  asks: [0, 1, 2].map((i) => ({ price: ask + i, size })),
  state: "MARKET_STATE_OPEN",
});

function ctx(): MockCtx {
  const c = {
    emit: (m: ServerMsg) => emitted.push(m),
    now: () => now,
    current: () => win,
    next: () => win,
    getWindow: () => win,
    windows: () => [win],
    lastTick: () => ({ ts: now, price: btcPx, sources: {} }),
    lastOdds: (): OddsSnap => ({ ts: now, windowId: "w1", upBid: book.bids[0].price, upAsk: book.asks[0].price, downBid: null, downAsk: null, upMid: null }),
    lastFair: () => null,
    book: () => book,
    settings: () => settings,
    putSettings: () => settings,
    bars: () => [],
    odds: () => [],
    fair: () => [],
    priceAt: () => btcPx,
  } as unknown as MockCtx;
  const riskState = () =>
    computeRiskState({
      limits: settings.risk!,
      mode: settings.mode,
      now,
      ...c.modules.trading.riskInputs("w1"),
      killSwitch: false,
      tradingDisabledUntil: null,
      disabledReason: null,
    });
  c.modules = {
    risk: { name: "risk", state: riskState, check: (o: Parameters<typeof checkOrder>[1]) => checkOrder(riskState(), o, now), refresh: riskState, kill: () => ({}) },
    live: { name: "live", status: () => ({}), isSessionConfirmed: (s: string | null | undefined) => confirmed && s === "sess" },
  } as unknown as MockCtx["modules"];
  return c;
}

const call = async <T>(method: string, path: string, req: Partial<MockRequest> = {}): Promise<T> => {
  const r = t.routes!.find((x) => x.method === method && x.path === path)!;
  return (await r.handler({ params: {}, query: {}, body: null, ...req })) as T;
};
const place = (body: object) => call<PlaceOrderResponse>("POST", "/orders", { body: { maxSlippageCents: 2, ...body } });
const rejects = async (p: Promise<unknown>, code: string) =>
  assert.rejects(p, (e: unknown) => e instanceof ApiError && e.code === code);

beforeEach(() => {
  now = T0 + 60_000;
  btcPx = 67050;
  book = mkBook(24, 26);
  settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "", risk: { ...DEFAULT_RISK_LIMITS } };
  emitted = [];
  confirmed = false;
  const c = ctx();
  t = createMockTrading(c);
  (c.modules as { trading: MockTrading }).trading = t;
});

test("marketable buy walks the asks; position, fills and messages are emitted", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 30 });
  // 100 @ 26 = $26, then $4 / 0.27 = 14.8 @ 27
  assert.equal(r.order.status, "filled");
  assert.equal(r.order.filledQty, 114.8);
  assert.equal(r.fills.length, 1);
  assert.equal(r.position!.qty, 114.8);
  assert.equal(r.position!.markCents, 24);
  assert.ok(Math.abs(r.position!.costUsd - 30) < 0.011);
  assert.ok(r.fills[0].slippageCents! > 0);
  assert.equal(r.fills[0].feeUsd, null);
  const types = emitted.map((m) => m.type);
  for (const k of ["orders", "fills", "positions"]) assert.ok(types.includes(k as ServerMsg["type"]), k);
});

test("down buys mirror the Up book and fees apply when set", async () => {
  settings.feeCentsPerContract = 1;
  const r = await place({ side: "down", type: "marketable", usd: 7.6 });
  assert.equal(r.order.avgFillCents, 76);
  assert.equal(r.order.filledQty, 10);
  assert.equal(r.fills[0].feeUsd, 0.1);
});

test("limit below the ask rests, then fills at the limit when the book crosses", async () => {
  const r = await place({ side: "up", type: "limit", usd: 10, limitCents: 20 });
  assert.equal(r.order.status, "open");
  assert.equal(r.order.qty, 50);
  assert.equal(r.position, null);
  const open = await call<Order[]>("GET", "/orders", { query: { status: "open" } });
  assert.equal(open.length, 1);
  book = mkBook(18, 20);
  t.onSecond!(now);
  const pos = t.positions().find((p) => p.status === "open")!;
  assert.equal(pos.qty, 50);
  assert.equal(pos.avgCents, 20);
  assert.ok(emitted.some((m) => m.type === "notice" && m.data.title === "Limit order filled"));
});

test("cancel a resting order", async () => {
  const r = await place({ side: "up", type: "limit", usd: 10, limitCents: 20 });
  const o = await call<Order>("DELETE", "/orders/:id", { params: { id: r.order.id } });
  assert.equal(o.status, "cancelled");
  assert.equal(t.riskInputs("w1").openExposureUsd, 0);
});

test("OCO: take profit exits the whole position and closes the bracket", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 13, bracket: { tpCents: 40, slCents: 15, btcStop: null, timeExitSec: null } });
  assert.equal(r.bracket!.status, "active");
  assert.equal(r.bracket!.maxSlippageCents, 2);
  book = mkBook(41, 43);
  t.onSecond!(now);
  const p = t.positions()[0];
  assert.equal(p.status, "closed");
  assert.ok(p.realizedPnlUsd > 0);
  const bs = await call<Bracket[]>("GET", "/brackets");
  assert.equal(bs[0].status, "done");
  assert.equal(bs[0].triggerReason, "tp");
  assert.ok(t.riskInputs("w1").dailyRealizedPnlUsd > 0);
});

test("BTC stop and time exit legs trigger; editing / removing a bracket", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 13 });
  const id = r.position!.id;
  const b = await call<Bracket>("PUT", "/positions/:id/bracket", { params: { id }, body: { tpCents: null, slCents: null, btcStop: 67000, timeExitSec: null } });
  assert.equal(b.btcStop, 67000);
  assert.equal(await call("PUT", "/positions/:id/bracket", { params: { id }, body: null }), null);
  await call("PUT", "/positions/:id/bracket", { params: { id }, body: { tpCents: null, slCents: null, btcStop: null, timeExitSec: 30 } });
  now = win.endTs - 20_000;
  t.onSecond!(now);
  assert.equal(t.positions()[0].status, "closed");
  btcPx = 66000; // no more brackets: nothing else happens
  t.onSecond!(now);
});

test("close position sells into the bids", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 13 });
  const c = await call<PlaceOrderResponse>("POST", "/positions/:id/close", { params: { id: r.position!.id }, body: {} });
  assert.equal(c.order.action, "sell");
  assert.equal(c.order.reason, "close");
  assert.equal(c.position!.status, "closed");
  assert.equal(c.position!.realizedPnlUsd, -1); // 50 @ 26 -> 50 @ 24
});

test("settlement at rollover pays 100 / 0 and cancels resting orders", async () => {
  await place({ side: "up", type: "marketable", usd: 13 });
  await place({ side: "down", type: "limit", usd: 5, limitCents: 50 });
  t.onRollover!({ ...win, status: "settled", result: "up" }, { ...win, id: "w2" });
  const p = t.positions()[0];
  assert.equal(p.status, "settled");
  assert.equal(p.realizedPnlUsd, 37); // 50 * (100 - 26) / 100
  const orders = await call<Order[]>("GET", "/orders");
  assert.ok(orders.every((o) => o.status !== "open"));
});

test("risk limits reject (403) and are recorded as rejected orders", async () => {
  await rejects(place({ side: "up", type: "marketable", usd: 300 }), "risk_rejected");
  const orders = await call<Order[]>("GET", "/orders");
  assert.equal(orders[0].status, "rejected");
});

test("live mode needs a confirmed session", async () => {
  settings.mode = "live";
  await rejects(place({ side: "up", type: "marketable", usd: 13 }), "live_confirm_required");
  confirmed = true;
  const r = await place({ side: "up", type: "marketable", usd: 13, liveSession: "sess" });
  assert.equal(r.order.mode, "live");
});

test("validation and kill helpers", async () => {
  await rejects(place({ side: "sideways", type: "marketable", usd: 5 }), "validation");
  await rejects(place({ side: "up", type: "limit", usd: 5 }), "validation");
  await place({ side: "up", type: "marketable", usd: 13 });
  await place({ side: "up", type: "limit", usd: 5, limitCents: 10 });
  assert.equal(t.cancelAll("kill"), 1);
  assert.equal(t.flattenAll("kill"), 1);
  assert.equal(t.positions().filter((p) => p.status === "open").length, 0);
});

test("entry fees are realized at once: position and daily realized P&L include them", async () => {
  settings.feeCentsPerContract = 1;
  const r = await place({ side: "up", type: "marketable", usd: 13 }); // 50 @ 26, entry fee $0.50
  assert.equal(r.position!.realizedPnlUsd, -0.5);
  assert.equal(t.riskInputs("w1").dailyRealizedPnlUsd, -0.5);
  const c = await call<PlaceOrderResponse>("POST", "/positions/:id/close", { params: { id: r.position!.id }, body: {} });
  // 50 * (24 - 26) / 100 = -1, minus 0.50 exit fee, minus 0.50 entry fee
  assert.equal(c.position!.realizedPnlUsd, -2);
  assert.equal(t.riskInputs("w1").dailyRealizedPnlUsd, -2);
});

test("entry fees count at settlement too", async () => {
  settings.feeCentsPerContract = 2;
  await place({ side: "up", type: "marketable", usd: 13 }); // 50 @ 26, fee $1
  t.onRollover!({ ...win, status: "settled", result: "down" }, { ...win, id: "w2" });
  assert.equal(t.positions()[0].realizedPnlUsd, -14); // -13 cost - 1 fee
  assert.equal(t.riskInputs("w1").dailyRealizedPnlUsd, -14);
});

test("a partial bracket exit keeps the bracket triggered and sells the rest on a later second", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 13, bracket: { tpCents: 40, slCents: 15, btcStop: null, timeExitSec: null } });
  const id = r.position!.id;
  book = mkBook(41, 43, 10); // 41 / 40 / 39 x 10 within the 2¢ cap: 30 of 50 sell
  t.onSecond!(now);
  let p = t.positions().find((x) => x.id === id)!;
  assert.equal(p.status, "open");
  assert.equal(p.qty, 20);
  let b = (await call<Bracket[]>("GET", "/brackets")).find((x) => x.positionId === id)!;
  assert.equal(b.status, "triggered");
  await rejects(call("PUT", "/positions/:id/bracket", { params: { id }, body: { tpCents: 60, slCents: null, btcStop: null, timeExitSec: null } }), "validation");
  book = mkBook(30, 32, 100); // the retry sells regardless of the TP level (it already triggered)
  now += 1000;
  t.onSecond!(now);
  p = t.positions().find((x) => x.id === id)!;
  assert.equal(p.status, "closed");
  b = (await call<Bracket[]>("GET", "/brackets")).find((x) => x.positionId === id)!;
  assert.equal(b.status, "done");
});

test("a repeated clientOrderId returns the first order instead of buying again", async () => {
  const a = await place({ side: "up", type: "marketable", usd: 13, clientOrderId: "web-k1" });
  const b = await place({ side: "up", type: "marketable", usd: 13, clientOrderId: "web-k1" });
  assert.equal(b.order.id, a.order.id);
  assert.equal(t.positions()[0].qty, 50);
  const c = await place({ side: "up", type: "marketable", usd: 13, clientOrderId: "web-k2" });
  assert.notEqual(c.order.id, a.order.id);
});

test("bracket rules match the backend: 3s time exit minimum, TP above SL, no leg through the bid", async () => {
  await rejects(place({ side: "up", type: "marketable", usd: 13, bracket: { tpCents: null, slCents: null, btcStop: null, timeExitSec: 2 } }), "validation");
  await rejects(place({ side: "up", type: "marketable", usd: 13, bracket: { tpCents: 20, slCents: 30, btcStop: null, timeExitSec: null } }), "validation");
  const r = await place({ side: "up", type: "marketable", usd: 13 });
  const id = r.position!.id; // Up bid 24
  const put = (b: object) => call("PUT", "/positions/:id/bracket", { params: { id }, body: { tpCents: null, slCents: null, btcStop: null, timeExitSec: null, ...b } });
  await rejects(put({ tpCents: 24 }), "validation");
  await rejects(put({ slCents: 24 }), "validation");
  await rejects(put({ timeExitSec: 1 }), "validation");
  assert.equal(((await put({ tpCents: 25, slCents: 23, timeExitSec: 3 })) as Bracket).timeExitSec, 3);
});

test("GET /brackets?status=active includes a triggered bracket whose exit is still working", async () => {
  const r = await place({ side: "up", type: "marketable", usd: 30, bracket: { tpCents: null, slCents: 20, btcStop: null, timeExitSec: null, maxSlippageCents: 0 } });
  book = { ...mkBook(20, 22, 10) }; // SL fires, only 10 contracts at the bid within a 0 cap
  t.onSecond!(now);
  const active = await call<Bracket[]>("GET", "/brackets", { query: { status: "active" } });
  assert.equal(active.length, 1);
  assert.equal(active[0].status, "triggered");
  assert.equal(active[0].positionId, r.position!.id);
});

test("kill flatten that cannot finish arms a triggered kill bracket that retries until flat", async () => {
  await place({ side: "up", type: "marketable", usd: 30 }); // 114.8 contracts
  book = mkBook(24, 26, 10); // only 30 contracts of bids
  assert.equal(t.flattenAll("kill"), 0);
  const p = t.positions()[0];
  assert.equal(p.status, "open");
  const kb = (await call<Bracket[]>("GET", "/brackets")).find((b) => b.triggerReason === "kill")!;
  assert.equal(kb.status, "triggered");
  assert.ok(emitted.some((m) => m.type === "notice" && m.data.title === "Kill switch: not flat yet"));
  book = mkBook(24, 26, 500);
  t.onSecond!(now);
  assert.equal(t.positions()[0].status, "closed");
  assert.equal((await call<Bracket[]>("GET", "/brackets")).find((b) => b.id === kb.id)!.status, "done");
});

test("cancelRestingBuys cancels only that mode's resting buys", async () => {
  await place({ side: "up", type: "limit", usd: 5, limitCents: 10 });
  assert.equal(t.cancelRestingBuys!("live"), 0);
  assert.equal(t.cancelRestingBuys!("paper"), 1);
  assert.ok((await call<Order[]>("GET", "/orders")).every((o) => o.status === "cancelled"));
});
