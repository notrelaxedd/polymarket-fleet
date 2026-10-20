// Paper-fill simulation (spec: "Unit tests: ... paper-fill simulation"): book walking, slippage caps, the
// derived Down book, fees / [FEE], IOC vs resting limit semantics and resting-order matching.
import { describe, expect, it } from "vitest";
import type { BookLevels, Order } from "../../shared/protocol.ts";
import { sideBook, simulateFill } from "../../shared/paper.ts";
import { clampLimit, floorQty, freshCrossingQty, marketableLimit, qtyForUsd, restingFillQty, walkBook } from "../src/trading/fill.ts";
import { PaperExecutor, bookTradeable, paperFee } from "../src/trading/paper.ts";

const BOOK: BookLevels = {
  ts: 1,
  windowId: "w1",
  instrument: "m:long",
  bids: [
    { price: 40, size: 100 },
    { price: 39, size: 200 },
    { price: 38, size: 500 },
  ],
  asks: [
    { price: 42, size: 50 },
    { price: 43, size: 100 },
    { price: 45, size: 1000 },
  ],
  state: "MARKET_STATE_OPEN",
};

const order = (o: Partial<Order>): Order => ({
  id: "o1",
  clientOrderId: null,
  externalId: null,
  mode: "paper",
  windowId: "w1",
  instrument: "m:long",
  side: "up",
  action: "buy",
  type: "marketable",
  limitCents: 44,
  maxSlippageCents: 2,
  qty: 100,
  notionalUsd: 30,
  filledQty: 0,
  avgFillCents: null,
  feesUsd: 0,
  status: "pending",
  reason: "manual",
  bracketId: null,
  positionId: null,
  error: null,
  createdTs: 0,
  updatedTs: 0,
  ...o,
});

const ctx = (fee: number | null = null, book: BookLevels | null = BOOK) => ({
  window: {
    id: "w1",
    title: "w1",
    startTs: 0,
    endTs: 900_000,
    priceToBeat: 67000,
    upInstrument: "m:long",
    downInstrument: "m:short",
    status: "live" as const,
    result: null,
    settlePrice: null,
  },
  book,
  odds: null,
  feeCentsPerContract: fee,
});

describe("walkBook", () => {
  it("spends a USD budget level by level in 0.1 contract steps", () => {
    const r = walkBook({ action: "buy", levels: BOOK.asks, limitCents: 44, budgetUsd: 30 });
    // 42 x 50 = $21, then $9 / 0.43 = 20.93 -> 20.9 contracts ($8.987); $0.013 left cannot buy 0.1 @ 45
    expect(r.executions).toEqual([
      { price: 42, size: 50 },
      { price: 43, size: 20.9 },
    ]);
    expect(r.qty).toBeCloseTo(70.9, 9);
    expect(r.notionalUsd).toBeCloseTo(29.987, 9);
    expect(r.avgCents).toBeCloseTo((29.987 * 100) / 70.9, 6);
    expect(r.leftUsd).toBeCloseTo(0.013, 9);
    expect(r.stoppedBy).toBe("done");
  });

  it("stops at the slippage cap (limit) and reports the unspent budget", () => {
    const r = walkBook({ action: "buy", levels: BOOK.asks, limitCents: 44, budgetUsd: 100 });
    expect(r.qty).toBe(150); // 50 @ 42 + 100 @ 43; 45 is past the 44 cap
    expect(r.notionalUsd).toBeCloseTo(64, 9);
    expect(r.leftUsd).toBeCloseTo(36, 9);
    expect(r.stoppedBy).toBe("limit");
  });

  it("reports a book that runs out before the request is done", () => {
    const r = walkBook({ action: "buy", levels: BOOK.asks, limitCents: Infinity, budgetUsd: 10_000 });
    expect(r.qty).toBe(1150);
    expect(r.stoppedBy).toBe("depth");
  });

  it("sells a contract count down the bids to the limit", () => {
    const r = walkBook({ action: "sell", levels: BOOK.bids, limitCents: 39, qty: 250 });
    expect(r.executions).toEqual([
      { price: 40, size: 100 },
      { price: 39, size: 150 },
    ]);
    expect(r.notionalUsd).toBeCloseTo(98.5, 9);
    expect(r.avgCents).toBeCloseTo(39.4, 9);
    expect(r.stoppedBy).toBe("done");
    const capped = walkBook({ action: "sell", levels: BOOK.bids, limitCents: 39.5, qty: 250 });
    expect(capped.qty).toBe(100);
    expect(capped.leftQty).toBe(150);
    expect(capped.stoppedBy).toBe("limit");
  });

  it("sells fractional live sizes exactly and skips dust levels on buys", () => {
    expect(walkBook({ action: "sell", levels: BOOK.bids, limitCents: 1, qty: 33.3333 }).qty).toBeCloseTo(33.3333, 6);
    const dust = walkBook({
      action: "buy",
      levels: [
        { price: 41, size: 0.05 },
        { price: 42, size: 10 },
      ],
      limitCents: 50,
      qty: 5,
    });
    expect(dust.executions).toEqual([{ price: 42, size: 5 }]);
  });

  it("matches shared simulateFill on a USD-budget buy (web mock parity)", () => {
    const a = walkBook({ action: "buy", levels: BOOK.asks, limitCents: 44, budgetUsd: 30 });
    const b = simulateFill({ action: "buy", levels: BOOK.asks, usd: 30, limitCents: 44, feeCentsPerContract: null });
    expect(a.qty).toBeCloseTo(b.qty, 9);
    expect(a.avgCents!).toBeCloseTo(b.avgCents!, 6);
    expect(b.slippageCents!).toBeCloseTo(a.avgCents! - 42, 6);
  });
});

describe("price helpers", () => {
  it("builds marketable limits on the 0.1 grid, clamped to 0.1..99.9", () => {
    expect(marketableLimit("buy", 42, 2)).toBe(44);
    expect(marketableLimit("sell", 40, 3)).toBe(37);
    expect(marketableLimit("buy", 98.5, 3)).toBe(99.9);
    expect(marketableLimit("sell", 1, 5)).toBe(0.1);
    expect(clampLimit(41.26)).toBe(41.3);
    expect(floorQty(70.99)).toBe(70.9);
    expect(floorQty(0.3 * 3)).toBe(0.9);
    expect(qtyForUsd(20, 40)).toBe(50);
    expect(qtyForUsd(0.03, 40)).toBe(0);
  });

  it("derives the Down book from the Up book", () => {
    const d = sideBook(BOOK, "down");
    expect(d.asks[0]).toEqual({ price: 60, size: 100 });
    expect(d.bids[0]).toEqual({ price: 58, size: 50 });
  });

  it("counts crossing size for resting orders", () => {
    expect(restingFillQty("buy", BOOK.asks, 41, 100)).toBe(0);
    expect(restingFillQty("buy", BOOK.asks, 43, 100)).toBe(100);
    expect(restingFillQty("buy", BOOK.asks, 43, 500)).toBe(150);
    expect(restingFillQty("sell", BOOK.bids, 39, 1000)).toBe(300);
  });

  it("fresh crossing size: only growth since the baseline, minus what older orders took from the snapshot", () => {
    const used = new Map<number, number>();
    // no baseline: every crossing level is new
    const a = freshCrossingQty("buy", BOOK.asks, 43, 120, new Map(), used);
    expect(a.qty).toBe(120);
    expect([...a.takes]).toEqual([
      [42, 50],
      [43, 70],
    ]);
    expect([...a.seen]).toEqual([
      [42, 50],
      [43, 100],
    ]);
    // a second order on the same snapshot only gets what is left (30 at 43)
    const b = freshCrossingQty("buy", BOOK.asks, 43, 100, new Map(), used);
    expect(b.qty).toBe(30);
    // the same book again against its own baseline: nothing new
    expect(freshCrossingQty("buy", BOOK.asks, 43, 100, a.seen, new Map()).qty).toBe(0);
    // a level that grew offers the growth; a level that vanished and came back counts in full
    const grown = [
      { price: 42, size: 80 },
      { price: 43, size: 100 },
    ];
    expect(freshCrossingQty("buy", grown, 43, 100, a.seen, new Map()).qty).toBe(30);
    expect(freshCrossingQty("buy", BOOK.asks, 43, 500, new Map([[43, 100]]), new Map()).qty).toBe(50);
  });

  it("prices fees per contract from the settings override; unknown = null ([FEE])", () => {
    expect(paperFee(42, 50, null)).toBeNull();
    expect(paperFee(42, 50, 1)).toBeCloseTo(0.5, 9);
    expect(bookTradeable({ state: null })).toBe(true);
    expect(bookTradeable({ state: "MARKET_STATE_SUSPENDED" })).toBe(false);
  });
});

describe("PaperExecutor", () => {
  const ex = new PaperExecutor(() => 1234);

  it("fills a marketable buy against the real book, one fill per level, fee unknown = null", () => {
    const r = ex.execute(order({}), ctx(null));
    expect(r.status).toBe("filled");
    expect(r.fills.map((f) => [f.priceCents, f.qty, f.feeUsd, f.ts])).toEqual([
      [42, 50, null, 1234],
      [43, 20.9, null, 1234],
    ]);
  });

  it("applies the fee override per fill", () => {
    const r = ex.execute(order({}), ctx(1));
    expect(r.fills.map((f) => f.feeUsd)).toEqual([0.5, 0.209]);
  });

  it("cancels the IOC remainder past the slippage cap", () => {
    const r = ex.execute(order({ notionalUsd: 100 }), ctx());
    expect(r.status).toBe("cancelled");
    expect(r.fills.reduce((s, f) => s + f.qty, 0)).toBe(150);
    expect(r.error).toMatch(/partially filled.*2¢ slippage cap/);
    const none = ex.execute(order({ limitCents: 41 }), ctx());
    expect(none.status).toBe("cancelled");
    expect(none.fills).toEqual([]);
    expect(none.error).toMatch(/nothing filled/);
  });

  it("buys Down from the flipped book", () => {
    const r = ex.execute(order({ side: "down", instrument: "m:short", limitCents: 61, notionalUsd: 30 }), ctx());
    expect(r.status).toBe("filled");
    expect(r.fills).toHaveLength(1);
    expect(r.fills[0]).toMatchObject({ priceCents: 60, qty: 50 });
  });

  it("sells (exit) down the side's bids", () => {
    const r = ex.execute(order({ action: "sell", qty: 120, notionalUsd: null, limitCents: 37 }), ctx());
    expect(r.status).toBe("filled");
    expect(r.fills.map((f) => [f.priceCents, f.qty])).toEqual([
      [40, 100],
      [39, 20],
    ]);
  });

  it("limit orders take what crosses now and rest the rest", () => {
    const rest = ex.execute(order({ type: "limit", limitCents: 41, qty: 50 }), ctx());
    expect(rest.status).toBe("open");
    expect(rest.fills).toEqual([]);
    const part = ex.execute(order({ type: "limit", limitCents: 42, qty: 80 }), ctx());
    expect(part.status).toBe("partial");
    expect(part.fills).toEqual([expect.objectContaining({ priceCents: 42, qty: 50 })]);
    const full = ex.execute(order({ type: "limit", limitCents: 43, qty: 80 }), ctx());
    expect(full.status).toBe("filled");
  });

  it("rejects without a book, for another window, or when the market is not open", () => {
    expect(ex.execute(order({}), ctx(null, null)).status).toBe("rejected");
    expect(ex.execute(order({ windowId: "w0" }), ctx()).status).toBe("rejected");
    const r = ex.execute(order({}), ctx(null, { ...BOOK, state: "MARKET_STATE_HALTED" }));
    expect(r).toMatchObject({ status: "rejected", error: expect.stringContaining("HALTED") });
  });

  it("matches a resting limit at its own price when the book crosses", () => {
    const fresh = new PaperExecutor(() => 1234);
    const o = order({ id: "r1", type: "limit", limitCents: 42.5, qty: 80, filledQty: 30, notionalUsd: 34 });
    const f = fresh.matchResting(o, BOOK, 1);
    expect(f).toMatchObject({ priceCents: 42.5, qty: 50, feeUsd: 0.5 });
    expect(fresh.matchResting({ ...o, id: "r2", limitCents: 41 }, BOOK, null)).toBeNull();
    expect(fresh.matchResting({ ...o, id: "r3", mode: "live" }, BOOK, null)).toBeNull();
  });

  it("never fills a resting limit twice from the same displayed liquidity", () => {
    const fresh = new PaperExecutor(() => 1234);
    const thin: BookLevels = { ...BOOK, asks: [{ price: 39, size: 3 }, ...BOOK.asks] };
    // 100 @ 40 takes the 3 offered at 39 as a taker and rests 97
    const placed = order({ id: "big", type: "limit", limitCents: 40, qty: 100 });
    const r = fresh.execute(placed, { window: {} as never, book: thin, odds: null, feeCentsPerContract: null });
    expect(r).toMatchObject({ status: "partial", fills: [expect.objectContaining({ priceCents: 39, qty: 3 })] });
    const resting = { ...placed, filledQty: 3 };
    // the same 3 @ 39 in every later snapshot is not new liquidity
    for (let i = 0; i < 5; i++) expect(fresh.matchResting(resting, { ...thin, ts: 2 + i }, null)).toBeNull();
    // the level grows by 7: only those 7 fill
    const grown: BookLevels = { ...thin, ts: 10, asks: [{ price: 39, size: 10 }, ...BOOK.asks] };
    expect(fresh.matchResting(resting, grown, null)).toMatchObject({ qty: 7, priceCents: 40 });
    // two resting orders share one snapshot: the older takes it, the younger gets the rest
    const a = order({ id: "a", type: "limit", limitCents: 40, qty: 6 });
    const b = order({ id: "b", type: "limit", limitCents: 40, qty: 6 });
    const use = new Map();
    const shared: BookLevels = { ...thin, ts: 11, asks: [{ price: 39.5, size: 8 }, ...BOOK.asks] };
    expect(fresh.matchResting(a, shared, null, use)?.qty).toBe(6);
    expect(fresh.matchResting(b, shared, null, use)?.qty).toBe(2);
  });
});

describe("shared simulateFill edge cases", () => {
  it("skips a dust level instead of stopping, and buys a contract count when no usd is given", () => {
    const levels = [
      { price: 40, size: 0.05 },
      { price: 41, size: 10 },
    ];
    const byUsd = simulateFill({ action: "buy", levels, usd: 4.1, limitCents: 45, feeCentsPerContract: null });
    expect(byUsd.qty).toBe(10);
    expect(byUsd.executions).toEqual([{ price: 41, size: 10 }]);
    const byQty = simulateFill({ action: "buy", levels, qty: 5, limitCents: 45, feeCentsPerContract: 1 });
    expect(byQty).toMatchObject({ qty: 5, complete: true, feeUsd: 0.05 });
    expect(byQty.avgCents).toBeCloseTo(41, 9);
    const capped = simulateFill({ action: "buy", levels, qty: 20, limitCents: 45, feeCentsPerContract: null });
    expect(capped).toMatchObject({ qty: 10, complete: false });
  });
});
