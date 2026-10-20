// Trading engine (spec: "Unit tests: ... OCO bracket logic"): bracket legs + priority, the backend watcher
// (TP / SL / BTC stop / time exit, OCO cancel, capped exits that retry), positions + P&L, settlement,
// resting limits, risk hooks, live gating, private-WS updates and the REST routes.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type {
  BookLevel,
  BookLevels,
  BtcTick,
  MarketWindow,
  OddsSnap,
  Order,
  ServerMsg,
  Settings,
  StateSnapshot,
} from "../../shared/protocol.ts";
import { DEFAULT_RISK_LIMITS } from "../../shared/protocol.ts";
import { evaluateBracket } from "../../shared/paper.ts";
import type { RiskCheckResult, RiskOrderCheck } from "../../shared/risk.ts";
import { loadConfig } from "../src/config.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import type { ExecContext, ExecResult, Executor, ModuleDeps, Services } from "../src/modules/types.ts";
import { buildServer } from "../src/server/index.ts";
import { createTrading } from "../src/trading/index.ts";
import { EXIT_RETRY_MS } from "../src/trading/service.ts";

const T0 = Date.UTC(2026, 9, 20, 14, 31, 0); // 10:31 ET
const WIN: MarketWindow = {
  id: "btc-15m-1030",
  title: "BTC Up or Down 10:30",
  startTs: T0 - 60_000,
  endTs: T0 + 840_000,
  priceToBeat: 67_000,
  upInstrument: "btc-15m-1030:long",
  downInstrument: "btc-15m-1030:short",
  status: "live",
  result: null,
  settlePrice: null,
};

class FakeEngine extends EventEmitter {
  win: MarketWindow | null = WIN;
  odds: OddsSnap | null = null;
  book: BookLevels | null = null;
  tick: BtcTick | null = null;
  settings: Settings = {
    mode: "paper",
    volLookbackMin: 30,
    feeCentsPerContract: null,
    feeNote: "",
    risk: DEFAULT_RISK_LIMITS,
    liveAvailable: false,
  };
  constructor(private readonly clock: () => number) {
    super();
  }
  snapshot(): StateSnapshot {
    return {
      serverTime: this.clock(),
      settings: this.getSettings(),
      current: this.win,
      next: null,
      recent: [],
      lastTick: this.tick,
      lastOdds: this.odds,
      lastFair: null,
      feeds: [],
    };
  }
  feeds() {
    return [];
  }
  getSettings(): Settings {
    return { ...this.settings };
  }
  updateSettings(p: Partial<Settings>): Settings {
    Object.assign(this.settings, p);
    return this.getSettings();
  }
  currentWindow() {
    return this.win;
  }
  nextWindow() {
    return null;
  }
  latestTick() {
    return this.tick;
  }
  latestOdds(now = this.clock()) {
    const o = this.odds;
    return o && this.win && o.windowId === this.win.id && now - o.ts <= 3000 ? o : null;
  }
  latestFair() {
    return null;
  }
  currentBook() {
    return this.book && this.book.windowId === this.win?.id ? this.book : null;
  }
  async refreshBook() {
    return this.currentBook();
  }
  priceAt() {
    return null;
  }
  liveAvailable() {
    return false;
  }
}

type MsgData<K extends ServerMsg["type"]> = Extract<ServerMsg, { type: K }>["data"];
type MsgItem<K extends ServerMsg["type"]> = MsgData<K> extends readonly (infer U)[] ? U : MsgData<K>;

const lv = (...pairs: [number, number][]): BookLevel[] => pairs.map(([price, size]) => ({ price, size }));
const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setup() {
  let t = T0;
  const engine = new FakeEngine(() => t);
  const db: TerminalDb = openDb(":memory:");
  db.upsertWindow(WIN, T0);
  const msgs: ServerMsg[] = [];
  const riskCalls: RiskOrderCheck[] = [];
  const risk = { result: { ok: true } as RiskCheckResult, refreshes: 0 };
  const live = { executor: null as Executor | null, sessions: new Set<string>() };
  const services = {} as Services;
  const deps: ModuleDeps = {
    config: loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv),
    db,
    engine,
    broadcast: (m) => msgs.push(m),
    logger: pino({ level: "silent" }),
    now: () => t,
    services,
  };
  const mod = createTrading(deps);
  Object.assign(services, {
    trading: mod.service,
    risk: {
      state: () => ({}) as never,
      check: (o: RiskOrderCheck) => {
        riskCalls.push(o);
        return risk.result;
      },
      refresh: () => {
        risk.refreshes++;
        return {} as never;
      },
      kill: async () => ({}) as never,
    },
    live: {
      status: () => ({}) as never,
      isSessionConfirmed: (s: string | null | undefined) => !!s && live.sessions.has(s),
      executor: () => live.executor,
    },
  } as Partial<Services>);
  cleanup.push(async () => {
    await mod.stop?.();
    db.close();
  });

  /** set the Up book (and matching top-of-book odds) at the current time */
  const setBook = (bids: BookLevel[], asks: BookLevel[], emit = false) => {
    engine.book = { ts: t, windowId: WIN.id, instrument: WIN.upInstrument, bids, asks, state: "MARKET_STATE_OPEN" };
    const ub = bids[0]?.price ?? null;
    const ua = asks[0]?.price ?? null;
    engine.odds = {
      ts: t,
      windowId: WIN.id,
      upBid: ub,
      upAsk: ua,
      downBid: ua == null ? null : 100 - ua,
      downAsk: ub == null ? null : 100 - ub,
      upMid: ub != null && ua != null ? (ub + ua) / 2 : (ub ?? ua),
    };
    if (emit) mod.core.onBook(engine.book);
  };
  const setBtc = (price: number) => (engine.tick = { ts: t, price, sources: {} });
  setBook(lv([40, 100], [39, 200], [38, 500]), lv([42, 50], [43, 100], [45, 1000]));

  return {
    engine,
    db,
    msgs,
    risk,
    riskCalls,
    live,
    mod,
    core: mod.core,
    svc: mod.service,
    setBook,
    setBtc,
    advance: (ms: number) => (t += ms),
    setTime: (ts: number) => (t = ts),
    now: () => t,
    /** every item broadcast with this message type (array payloads flattened) */
    sent: <K extends ServerMsg["type"]>(type: K): MsgItem<K>[] =>
      msgs.filter((m) => m.type === type).flatMap((m) => (Array.isArray(m.data) ? m.data : [m.data]) as unknown as MsgItem<K>[]),
  };
}

// ======================================================================= pure bracket logic

describe("evaluateBracket (OCO leg logic)", () => {
  const m = { now: 0, sideBid: 50 as number | null, btc: 67_000 as number | null, windowEndTs: 600_000 };
  const none = { tpCents: null, slCents: null, btcStop: null, timeExitSec: null };

  it("fires TP at bid >= tp and SL at bid <= sl, on the position side's bid", () => {
    expect(evaluateBracket({ ...none, tpCents: 50 }, "up", m)).toBe("tp");
    expect(evaluateBracket({ ...none, tpCents: 51 }, "up", m)).toBeNull();
    expect(evaluateBracket({ ...none, slCents: 50 }, "down", m)).toBe("sl");
    expect(evaluateBracket({ ...none, slCents: 49 }, "down", m)).toBeNull();
    expect(evaluateBracket({ ...none, tpCents: 1, slCents: 99 }, "up", { ...m, sideBid: null })).toBeNull();
  });

  it("BTC stop: Up exits below, Down exits above", () => {
    expect(evaluateBracket({ ...none, btcStop: 67_001 }, "up", m)).toBe("btc_stop");
    expect(evaluateBracket({ ...none, btcStop: 67_000 }, "up", m)).toBeNull();
    expect(evaluateBracket({ ...none, btcStop: 66_999 }, "down", m)).toBe("btc_stop");
    expect(evaluateBracket({ ...none, btcStop: 67_000 }, "down", m)).toBeNull();
    expect(evaluateBracket({ ...none, btcStop: 70_000 }, "up", { ...m, btc: null })).toBeNull();
  });

  it("time exit at T-n seconds", () => {
    expect(evaluateBracket({ ...none, timeExitSec: 599 }, "up", m)).toBeNull();
    expect(evaluateBracket({ ...none, timeExitSec: 600 }, "up", m)).toBe("time_exit");
  });

  it("one leg wins when several fire: sl > btc_stop > tp > time_exit", () => {
    const all = { tpCents: 40, slCents: 50, btcStop: 68_000, timeExitSec: 900 };
    expect(evaluateBracket(all, "up", m)).toBe("sl");
    expect(evaluateBracket({ ...all, slCents: null }, "up", m)).toBe("btc_stop");
    expect(evaluateBracket({ ...all, slCents: null, btcStop: null }, "up", m)).toBe("tp");
    expect(evaluateBracket({ ...all, slCents: null, btcStop: null, tpCents: null }, "up", m)).toBe("time_exit");
  });
});

// ======================================================================= paper orders + positions

describe("paper orders and positions", () => {
  it("marketable buy walks the real book, logs slippage, opens a position marked at the bid", async () => {
    const h = setup();
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 30, maxSlippageCents: 2 });
    expect(r.order).toMatchObject({ status: "filled", action: "buy", limitCents: 44, notionalUsd: 30, reason: "manual" });
    expect(r.order.qty).toBeCloseTo(70.9, 9);
    expect(r.fills.map((f) => [f.priceCents, f.qty, f.slippageCents, f.feeUsd])).toEqual([
      [42, 50, 0, null],
      [43, 20.9, 1, null],
    ]);
    const p = r.position!;
    expect(p).toMatchObject({ status: "open", side: "up", markCents: 40, instrument: WIN.upInstrument });
    expect(p.qty).toBeCloseTo(70.9, 9);
    expect(p.costUsd).toBeCloseTo(29.987, 6);
    expect(p.avgCents).toBeCloseTo(42.2948, 3);
    expect(p.unrealizedPnlUsd!).toBeCloseTo((70.9 * (40 - p.avgCents)) / 100, 3);
    expect(p.realizedPnlUsd).toBe(0); // fee unknown ([FEE]) counts 0
    // persisted + broadcast
    expect(h.svc.orders()).toHaveLength(1);
    expect(h.svc.fills()).toHaveLength(2);
    expect(h.sent("orders").at(-1)).toMatchObject({ id: r.order.id, status: "filled" });
    expect(h.sent("fills")).toHaveLength(2);
    expect(h.sent("positions").at(-1)).toMatchObject({ id: p.id, status: "open" });
    expect(h.risk.refreshes).toBeGreaterThan(0);
    expect(h.riskCalls[0]).toEqual({ addCostUsd: 30, windowCostUsd: 0, positionCostUsd: 0, reducing: false });
  });

  it("partial IOC: remainder past the cap is cancelled, the fill stands", async () => {
    const h = setup();
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 100, maxSlippageCents: 2 });
    expect(r.order.status).toBe("cancelled");
    expect(r.order.filledQty).toBe(150);
    expect(r.order.qty).toBe(230); // what $100 buys walking the whole book
    expect(r.order.error).toMatch(/remainder cancelled/);
    expect(r.position!.qty).toBe(150);
  });

  it("nothing fillable -> 409 no_market, the order is kept as cancelled", async () => {
    const h = setup();
    h.setBook(lv([40, 100]), []);
    await expect(h.svc.placeOrder({ side: "up", type: "marketable", usd: 10, maxSlippageCents: 2 })).rejects.toMatchObject({
      statusCode: 409,
      code: "no_market",
    });
    h.setBook(lv([40, 100]), lv([42, 1]));
    await expect(h.svc.placeOrder({ side: "up", type: "marketable", usd: 0.01, maxSlippageCents: 2 })).rejects.toMatchObject({
      statusCode: 400,
    });
  });

  it("Down buys from the flipped book and adds to the same position with a weighted average", async () => {
    const h = setup();
    const a = await h.svc.placeOrder({ side: "down", type: "marketable", usd: 30, maxSlippageCents: 1 });
    expect(a.fills.map((f) => [f.priceCents, f.qty])).toEqual([[60, 50]]);
    expect(a.position).toMatchObject({ side: "down", instrument: WIN.downInstrument, markCents: 58, unrealizedPnlUsd: -1 });
    h.setBook(lv([45, 100]), lv([47, 100]));
    const b = await h.svc.placeOrder({ side: "down", type: "marketable", usd: 11, maxSlippageCents: 1 });
    expect(b.position!.id).toBe(a.position!.id);
    expect(b.position!.qty).toBe(70);
    expect(b.position!.avgCents).toBeCloseTo((30 + 11) / 0.7, 6);
    expect(h.riskCalls[1]).toMatchObject({ addCostUsd: 11, positionCostUsd: 30, windowCostUsd: 30 });
    expect(h.svc.positions({ status: "open" })).toHaveLength(1);
  });

  it("fee override: fees are charged per fill and realized net", async () => {
    const h = setup();
    h.engine.settings.feeCentsPerContract = 1;
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 21, maxSlippageCents: 0 });
    expect(r.order.feesUsd).toBeCloseTo(0.5, 9);
    expect(r.fills[0]!.feeUsd).toBeCloseTo(0.5, 9);
    expect(r.position!.realizedPnlUsd).toBeCloseTo(-0.5, 9);
    expect(r.position!.feesUsd).toBeCloseTo(0.5, 9);
    h.setBook(lv([50, 100]), lv([52, 100]));
    const c = await h.svc.closePosition(r.position!.id);
    // 50 x (50 - 42) / 100 = 4 gross, minus 0.5 entry and 0.5 exit fees
    expect(c.position).toMatchObject({ status: "closed", qty: 0, costUsd: 0, unrealizedPnlUsd: 0 });
    expect(c.position!.realizedPnlUsd).toBeCloseTo(3, 9);
    expect(h.svc.riskInputs(WIN.id).dailyRealizedPnlUsd).toBeCloseTo(3, 9);
  });

  it("manual close sells at bid minus the cap and records the exit slippage", async () => {
    const h = setup();
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 21, maxSlippageCents: 2 });
    h.setBook(lv([48, 30], [47, 100]), lv([50, 100]));
    const c = await h.svc.closePosition(r.position!.id);
    expect(c.order).toMatchObject({ action: "sell", reason: "close", limitCents: 46, status: "filled", maxSlippageCents: 2 });
    expect(c.fills.map((f) => [f.priceCents, f.qty, f.slippageCents])).toEqual([
      [48, 30, 0],
      [47, 20, 1],
    ]);
    expect(c.position!.realizedPnlUsd).toBeCloseTo((30 * 6 + 20 * 5) / 100, 9);
    await expect(h.svc.closePosition(r.position!.id)).rejects.toMatchObject({ statusCode: 400 });
  });

  it("clientOrderId makes order placement idempotent", async () => {
    const h = setup();
    const req = { side: "up" as const, type: "marketable" as const, usd: 21, maxSlippageCents: 2, clientOrderId: "c-1" };
    const a = await h.svc.placeOrder(req);
    const b = await h.svc.placeOrder(req);
    expect(b.order.id).toBe(a.order.id);
    expect(h.svc.orders()).toHaveLength(1);
  });

  it("clientOrderId: a double submit in flight buys once (checked under the lock + UNIQUE index)", async () => {
    const h = setup();
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const orig = h.engine.refreshBook.bind(h.engine);
    h.engine.refreshBook = async () => {
      await gate; // a slow book refresh: the second request arrives while the first one waits
      return orig();
    };
    const req = { side: "up" as const, type: "marketable" as const, usd: 21, maxSlippageCents: 2, clientOrderId: "dbl" };
    const a = h.svc.placeOrder(req);
    const b = h.svc.placeOrder(req);
    const c = h.svc.placeOrder({ ...req, side: "down" }); // same key on the other side's lock
    release();
    const [ra, rb, rc] = await Promise.all([a, b, c]);
    expect(rb.order.id).toBe(ra.order.id);
    expect(rc.order.id).toBe(ra.order.id);
    expect(h.svc.orders()).toHaveLength(1);
    expect(h.svc.positions({ status: "open" })).toHaveLength(1);
    expect(h.svc.positions()[0]!.qty).toBe(50);
    // the index itself refuses a second row with the key
    expect(h.core.store.clientIdUnique).toBe(true);
    expect(() => h.core.store.insertOrder({ ...ra.order, id: "other" })).toThrow(/UNIQUE/);
  });

  it("requires a live window", async () => {
    const h = setup();
    h.setTime(WIN.endTs);
    await expect(h.svc.placeOrder({ side: "up", type: "marketable", usd: 10, maxSlippageCents: 2 })).rejects.toMatchObject({
      code: "no_market",
    });
  });
});

describe("resting limit orders", () => {
  it("rest, count as exposure, fill AT the limit when the book crosses, attach the bracket on fill", async () => {
    const h = setup();
    const r = await h.svc.placeOrder({
      side: "up",
      type: "limit",
      usd: 20,
      limitCents: 40,
      maxSlippageCents: 2,
      bracket: { tpCents: 60, slCents: 30, btcStop: null, timeExitSec: null },
    });
    expect(r.order).toMatchObject({ status: "open", qty: 50, filledQty: 0 });
    expect(r.position).toBeNull();
    expect(h.svc.riskInputs(WIN.id)).toMatchObject({ openExposureUsd: 20, windowLossUsedUsd: 20 });

    h.advance(1000);
    h.setBook(lv([38, 100]), lv([39.5, 30], [41, 100]), true);
    let o = h.svc.orders()[0]!;
    expect(o).toMatchObject({ status: "partial", filledQty: 30, avgFillCents: 40 });
    const f = h.svc.fills()[0]!;
    expect(f).toMatchObject({ priceCents: 40, qty: 30, slippageCents: -2 }); // vs the 42 ask at submit
    const pos = h.svc.positions({ status: "open" })[0]!;
    expect(pos.qty).toBe(30);
    const br = h.svc.brackets({ status: "active" })[0]!;
    expect(br).toMatchObject({ positionId: pos.id, tpCents: 60, slCents: 30, maxSlippageCents: 2, status: "active" });
    expect(pos.bracketId).toBe(br.id);

    // the same 30 @ 39.5 in the next snapshots was already taken: no fills from it again
    for (let i = 0; i < 3; i++) {
      h.advance(1000);
      h.setBook(lv([38, 100]), lv([39.5, 30], [41, 100]), true);
    }
    expect(h.svc.orders()[0]).toMatchObject({ status: "partial", filledQty: 30 });

    h.advance(1000);
    h.setBook(lv([38, 100]), lv([39.5, 50], [41, 100]), true); // +20 new at 39.5
    o = h.svc.orders()[0]!;
    expect(o).toMatchObject({ status: "filled", filledQty: 50 });
    expect(h.svc.positions({ status: "open" })[0]!.qty).toBe(50);
    expect(h.svc.brackets({ status: "all" })).toHaveLength(1); // same bracket, not a second one
  });

  it("cancel, and expiry at the window close", async () => {
    const h = setup();
    const a = await h.svc.placeOrder({ side: "up", type: "limit", usd: 10, limitCents: 30, maxSlippageCents: 2 });
    const c = await h.svc.cancelOrder(a.order.id);
    expect(c.status).toBe("cancelled");
    await expect(h.svc.cancelOrder(a.order.id)).rejects.toMatchObject({ statusCode: 400 });
    const b = await h.svc.placeOrder({ side: "down", type: "limit", usd: 10, limitCents: 30, maxSlippageCents: 2 });
    h.core.expireOrders();
    expect(h.svc.orders({ status: "open" })).toHaveLength(1);
    h.setTime(WIN.endTs);
    h.core.expireOrders();
    expect(h.svc.orders().find((o) => o.id === b.order.id)).toMatchObject({ status: "cancelled", error: "window closed" });
    expect(h.svc.riskInputs(WIN.id).openExposureUsd).toBe(0);
  });
});

// ======================================================================= the watcher (OCO)

async function openUp(h: ReturnType<typeof setup>, bracket: Parameters<typeof h.svc.setBracket>[1], usd = 21) {
  h.setBook(lv([40, 100], [39, 200]), lv([42, 1000]));
  const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd, maxSlippageCents: 2, bracket });
  expect(r.position!.qty).toBe((usd * 100) / 42);
  return r;
}

const sells = (h: ReturnType<typeof setup>) => h.svc.orders().filter((o: Order) => o.action === "sell");

describe("OCO bracket watcher", () => {
  it("TP fires on the bid, exits once, cancels the other legs (OCO)", async () => {
    const h = setup();
    const r = await openUp(h, { tpCents: 50, slCents: 30, btcStop: 60_000, timeExitSec: 30 });
    expect(r.bracket).toMatchObject({ status: "active", tpCents: 50, slCents: 30, maxSlippageCents: 2 });
    h.advance(500);
    h.setBook(lv([49, 100]), lv([51, 100]));
    h.core.evaluate();
    expect(sells(h)).toHaveLength(0);

    h.advance(500);
    h.setBook(lv([50, 100]), lv([52, 100]));
    h.core.evaluate();
    h.core.evaluate(); // a second evaluation while the exit runs must not double-sell
    await h.core.idle();
    const s = sells(h);
    expect(s).toHaveLength(1);
    expect(s[0]).toMatchObject({ reason: "tp", status: "filled", limitCents: 48, bracketId: r.bracket!.id, filledQty: 50 });
    const b = h.svc.brackets()[0]!;
    expect(b).toMatchObject({ status: "done", triggerReason: "tp", exitOrderId: s[0]!.id, triggeredTs: h.now() });
    const p = h.svc.positions()[0]!;
    expect(p).toMatchObject({ status: "closed", qty: 0 });
    expect(p.realizedPnlUsd).toBeCloseTo(4, 9);

    // the other legs are gone: SL / BTC stop / time exit conditions no longer do anything
    h.setBook(lv([20, 100]), lv([22, 100]));
    h.setBtc(50_000);
    h.setTime(WIN.endTs - 10_000);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(1);
    expect(h.sent("notice").map((n) => n.title)).toEqual(expect.arrayContaining(["Take profit triggered", "Take profit filled"]));
  });

  it("SL exit capped by slippage stays triggered and retries until flat", async () => {
    const h = setup();
    const r = await openUp(h, { tpCents: null, slCents: 35, btcStop: null, timeExitSec: null, maxSlippageCents: 1 });
    h.setBook(lv([35, 20], [34, 20], [33, 100]), lv([37, 100]));
    h.core.evaluate();
    await h.core.idle();
    let s = sells(h);
    expect(s).toHaveLength(1);
    expect(s[0]).toMatchObject({ reason: "sl", status: "cancelled", filledQty: 40, limitCents: 34 });
    expect(h.svc.brackets()[0]!.status).toBe("triggered");
    expect(h.svc.positions()[0]!.qty).toBe(10);

    h.core.evaluate(); // retry throttled
    await h.core.idle();
    expect(sells(h)).toHaveLength(1);

    h.advance(EXIT_RETRY_MS);
    h.setBook(lv([35, 20], [34, 20]), lv([37, 100]));
    h.core.evaluate();
    await h.core.idle();
    s = sells(h);
    expect(s).toHaveLength(2);
    expect(s[0]).toMatchObject({ reason: "sl", status: "filled", qty: 10 });
    const p = h.svc.positions()[0]!;
    expect(p.status).toBe("closed");
    expect(p.realizedPnlUsd).toBeCloseTo((20 * -7 + 20 * -8 + 10 * -7) / 100, 9);
    expect(h.svc.brackets()[0]!).toMatchObject({ id: r.bracket!.id, status: "done", exitOrderId: s[0]!.id });
  });

  it("BTC stop: an Up position exits when BTC drops below, a Down position when BTC rises above", async () => {
    const h = setup();
    await openUp(h, { tpCents: null, slCents: null, btcStop: 67_000, timeExitSec: null });
    h.setBtc(67_000);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(0);
    h.setBtc(66_999.5);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toEqual([expect.objectContaining({ reason: "btc_stop", side: "up", status: "filled" })]);

    const d = await h.svc.placeOrder({
      side: "down",
      type: "marketable",
      usd: 30,
      maxSlippageCents: 2,
      bracket: { tpCents: null, slCents: null, btcStop: 67_100, timeExitSec: null },
    });
    h.setBtc(67_050);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(1);
    h.setBtc(67_100.01);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)[0]).toMatchObject({ reason: "btc_stop", side: "down", positionId: d.position!.id });
    // a stale BTC tick never fires a stop
    const h2 = setup();
    await openUp(h2, { tpCents: null, slCents: null, btcStop: 67_000, timeExitSec: null });
    h2.setBtc(60_000);
    h2.advance(6_000);
    h2.setBook(lv([40, 100]), lv([42, 100]));
    h2.core.evaluate();
    await h2.core.idle();
    expect(sells(h2)).toHaveLength(0);
  });

  it("time exit at T-n seconds", async () => {
    const h = setup();
    await openUp(h, { tpCents: null, slCents: null, btcStop: null, timeExitSec: 60 });
    h.setTime(WIN.endTs - 61_000);
    h.setBook(lv([40, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(0);
    h.setTime(WIN.endTs - 60_000);
    h.setBook(lv([40, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toEqual([expect.objectContaining({ reason: "time_exit", status: "filled" })]);
  });

  it("dragging a leg updates the same bracket; removing it cancels; a manual close cancels it", async () => {
    const h = setup();
    const r = await openUp(h, { tpCents: 60, slCents: 30, btcStop: null, timeExitSec: null });
    const upd = h.svc.setBracket(r.position!.id, { tpCents: 65, slCents: 32, btcStop: null, timeExitSec: null });
    expect(upd).toMatchObject({ id: r.bracket!.id, tpCents: 65, slCents: 32, status: "active", maxSlippageCents: 2 });
    expect(() => h.svc.setBracket(r.position!.id, { tpCents: 30, slCents: 40, btcStop: null, timeExitSec: null })).toThrow(/above stop loss/);
    expect(() => h.svc.setBracket(r.position!.id, { tpCents: 120, slCents: null, btcStop: null, timeExitSec: null })).toThrow();
    expect(h.svc.setBracket(r.position!.id, null)).toBeNull();
    expect(h.svc.brackets()[0]).toMatchObject({ status: "cancelled" });
    expect(h.svc.positions()[0]!.bracketId).toBeNull();
    const again = h.svc.setBracket(r.position!.id, { tpCents: 70, slCents: null, btcStop: null, timeExitSec: null });
    expect(again!.id).not.toBe(r.bracket!.id);
    await h.svc.closePosition(r.position!.id);
    expect(h.svc.brackets().find((b) => b.id === again!.id)).toMatchObject({ status: "cancelled" });
    expect(() => h.svc.setBracket(r.position!.id, { tpCents: 70, slCents: null, btcStop: null, timeExitSec: null })).toThrow(/closed/);
  });

  it("a manual close racing a bracket exit never sells more than is held", async () => {
    const h = setup();
    const r = await openUp(h, { tpCents: null, slCents: 45, btcStop: null, timeExitSec: null });
    h.setBook(lv([40, 1000]), lv([42, 1000])); // bid 40 <= SL 45
    const [close] = await Promise.allSettled([h.svc.closePosition(r.position!.id), (h.core.evaluate(), h.core.idle())]);
    await h.core.idle();
    const sold = h.svc.fills().filter((f) => f.action === "sell").reduce((s, f) => s + f.qty, 0);
    expect(sold).toBe(50);
    expect(h.svc.positions()[0]).toMatchObject({ status: "closed", qty: 0 });
    expect(close.status).toBe("fulfilled");
    expect(h.svc.brackets()[0]).toMatchObject({ status: "done", triggerReason: "sl" });
    expect(h.sent("notice").some((n) => n.level === "error")).toBe(false);
  });

  it("exits that cannot trade (no bid) keep the bracket triggered and notify once", async () => {
    const h = setup();
    await openUp(h, { tpCents: null, slCents: null, btcStop: null, timeExitSec: 900 });
    h.setBook([], lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    h.advance(EXIT_RETRY_MS);
    h.setBook([], lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(0);
    expect(h.svc.brackets()[0]!.status).toBe("triggered");
    expect(h.sent("notice").filter((n) => n.title === "Time exit exit failed")).toHaveLength(1);
    h.advance(EXIT_RETRY_MS);
    h.setBook(lv([41, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)[0]).toMatchObject({ reason: "time_exit", status: "filled" });
    expect(h.svc.brackets()[0]!.status).toBe("done");
  });
});

// ======================================================================= settlement, risk, live

describe("settlement", () => {
  it("pays $1 per contract on the winning side and 0 otherwise; cancels brackets and resting orders", async () => {
    const h = setup();
    const up = await openUp(h, { tpCents: 90, slCents: 10, btcStop: null, timeExitSec: null });
    h.setBook(lv([40, 100]), lv([42, 100]));
    const down = await h.svc.placeOrder({ side: "down", type: "marketable", usd: 30, maxSlippageCents: 2 });
    const rest = await h.svc.placeOrder({ side: "up", type: "limit", usd: 5, limitCents: 20, maxSlippageCents: 2 });
    h.setTime(WIN.endTs + 5000);
    h.core.settle({ ...WIN, status: "settled", result: "up", settlePrice: 67_120 });
    const ps = h.svc.positions();
    const pu = ps.find((p) => p.id === up.position!.id)!;
    const pd = ps.find((p) => p.id === down.position!.id)!;
    expect(pu).toMatchObject({ status: "settled", qty: 0, markCents: 100, unrealizedPnlUsd: 0, closedTs: h.now() });
    expect(pu.realizedPnlUsd).toBeCloseTo(50 * 0.58, 9);
    expect(pd).toMatchObject({ status: "settled", markCents: 0 });
    expect(pd.realizedPnlUsd).toBeCloseTo(-30, 9);
    expect(h.svc.brackets()[0]).toMatchObject({ status: "cancelled" });
    expect(h.svc.orders().find((o) => o.id === rest.order.id)).toMatchObject({ status: "cancelled" });
    expect(h.svc.riskInputs(null).dailyRealizedPnlUsd).toBeCloseTo(-1, 9);
    // idempotent
    h.core.settle({ ...WIN, status: "settled", result: "up", settlePrice: 67_120 });
    expect(h.svc.positions().find((p) => p.id === pu.id)!.realizedPnlUsd).toBeCloseTo(29, 9);
    expect(h.sent("notice").filter((n) => n.title === "Settled UP")).toHaveLength(2);
  });

  it("settles positions of windows that settled while the worker was down", async () => {
    const h = setup();
    await openUp(h, null);
    h.db.upsertWindow({ ...WIN, status: "settled", result: "down", settlePrice: 66_900 }, h.now());
    h.engine.win = null;
    h.core.recover();
    expect(h.svc.positions()[0]).toMatchObject({ status: "settled", markCents: 0 });
    expect(h.svc.positions()[0]!.realizedPnlUsd).toBeCloseTo(-21, 9);
  });
});

describe("risk hooks", () => {
  it("risk_rejected -> 403, trading_disabled -> 423; the rejected order is recorded", async () => {
    const h = setup();
    h.risk.result = { ok: false, code: "risk_rejected", reason: "Max position $250" };
    await expect(h.svc.placeOrder({ side: "up", type: "marketable", usd: 300, maxSlippageCents: 2 })).rejects.toMatchObject({
      statusCode: 403,
      code: "risk_rejected",
      message: "Max position $250",
    });
    expect(h.svc.orders()[0]).toMatchObject({ status: "rejected", error: "Max position $250" });
    h.risk.result = { ok: false, code: "trading_disabled", reason: "Kill switch engaged" };
    await expect(h.svc.placeOrder({ side: "up", type: "marketable", usd: 3, maxSlippageCents: 2 })).rejects.toMatchObject({
      statusCode: 423,
    });
    expect(h.svc.positions()).toHaveLength(0);
  });

  it("exits never ask risk; kill = cancel all + flatten", async () => {
    const h = setup();
    await openUp(h, null);
    await h.svc.placeOrder({ side: "down", type: "limit", usd: 5, limitCents: 20, maxSlippageCents: 2 });
    const checks = h.riskCalls.length;
    h.risk.result = { ok: false, code: "trading_disabled", reason: "Kill switch engaged" };
    expect(await h.svc.cancelAll("kill")).toBe(1);
    expect(await h.svc.flattenAll("kill")).toBe(1);
    expect(h.riskCalls.length).toBe(checks);
    expect(sells(h)[0]).toMatchObject({ reason: "kill" });
    expect(h.svc.riskInputs(WIN.id)).toMatchObject({ openExposureUsd: 0, unrealizedPnlUsd: 0 });
  });

  it("riskInputs: realized today, unrealized at the bid, exposure and window cost at risk", async () => {
    const h = setup();
    await openUp(h, null); // 50 @ 42 = $21
    await h.svc.placeOrder({ side: "up", type: "limit", usd: 8, limitCents: 40, maxSlippageCents: 2 }); // rests $8
    expect(h.svc.riskInputs(WIN.id)).toEqual({
      dailyRealizedPnlUsd: 0,
      unrealizedPnlUsd: -1, // 50 x (40 - 42)
      openExposureUsd: 29,
      windowLossUsedUsd: 29,
    });
    expect(h.svc.riskInputs("other")).toMatchObject({ windowLossUsedUsd: 0 });
    h.engine.settings.mode = "live";
    expect(h.svc.riskInputs(WIN.id)).toEqual({ dailyRealizedPnlUsd: 0, unrealizedPnlUsd: 0, openExposureUsd: 0, windowLossUsedUsd: 0 });
  });
});

class FakeLive implements Executor {
  readonly mode = "live" as const;
  submitted: Order[] = [];
  cancelled: string[] = [];
  constructor(private readonly onSubmit: (o: Order, ctx: ExecContext) => ExecResult) {}
  async submit(o: Order, ctx: ExecContext) {
    this.submitted.push({ ...o });
    return this.onSubmit(o, ctx);
  }
  async cancel(o: Order) {
    this.cancelled.push(o.id);
  }
}

describe("live routing", () => {
  it("needs the live executor and a confirmed session for entries", async () => {
    const h = setup();
    h.engine.settings.mode = "live";
    const req = { side: "up" as const, type: "marketable" as const, usd: 4.2, maxSlippageCents: 2 };
    await expect(h.svc.placeOrder(req)).rejects.toMatchObject({ statusCode: 409, code: "live_unavailable" });
    const ex = new FakeLive(() => ({ externalId: "ex-1", status: "filled", fills: [{ externalId: "e1", priceCents: 42, qty: 10, feeUsd: 0.05, ts: h.now() }] }));
    h.live.executor = ex;
    await expect(h.svc.placeOrder({ ...req, liveSession: "nope" })).rejects.toMatchObject({ code: "live_confirm_required" });
    expect(ex.submitted).toHaveLength(0);
    h.live.sessions.add("s1");
    const r = await h.svc.placeOrder({ ...req, liveSession: "s1" });
    expect(ex.submitted[0]).toMatchObject({ mode: "live", limitCents: 44, notionalUsd: 4.2, qty: 10 });
    expect(r.order).toMatchObject({ mode: "live", externalId: "ex-1", status: "filled", feesUsd: 0.05 });
    expect(r.position).toMatchObject({ mode: "live", qty: 10 });
    // exits reduce risk: no session needed
    const ex2 = new FakeLive((o) => ({ externalId: "ex-2", status: "filled", fills: [{ externalId: "e2", priceCents: 40, qty: o.qty, feeUsd: 0.05, ts: h.now() }] }));
    h.live.executor = ex2;
    const c = await h.svc.closePosition(r.position!.id);
    expect(c.position).toMatchObject({ status: "closed" });
    expect(c.position!.realizedPnlUsd).toBeCloseTo(-0.2 - 0.1, 9);
  });

  it("applies private-WS fills (deduped), even when they beat the submit response", async () => {
    const h = setup();
    h.engine.settings.mode = "live";
    h.live.sessions.add("s1");
    const fill = { externalId: "e1", priceCents: 42, qty: 6, feeUsd: null, ts: h.now() };
    h.live.executor = new FakeLive(() => {
      h.svc.applyExternal({ externalId: "ex-9", fill, status: "partial" }); // arrives before the REST response
      return { externalId: "ex-9", status: "pending", fills: [] };
    });
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" });
    expect(r.order).toMatchObject({ status: "partial", filledQty: 6 });
    h.svc.applyExternal({ externalId: "ex-9", fill }); // duplicate
    h.svc.applyExternal({ externalId: "ex-9", fill: { ...fill, externalId: "e2", qty: 4 }, status: "filled" });
    const o = h.svc.orders()[0]!;
    expect(o).toMatchObject({ status: "filled", filledQty: 10 });
    expect(h.svc.fills()).toHaveLength(2);
    expect(h.svc.positions()[0]!.qty).toBe(10);
    h.svc.applyExternal({ externalId: "ex-9", status: "open" }); // late non-terminal update is ignored
    expect(h.svc.orders()[0]!.status).toBe("filled");
  });

  it("reconcile adjusts drifted quantities and adopts unknown exchange positions", async () => {
    const h = setup();
    h.engine.settings.mode = "live";
    h.live.sessions.add("s1");
    h.live.executor = new FakeLive((o) => ({ externalId: `x-${o.id}`, status: "filled", fills: [{ externalId: null, priceCents: 42, qty: 10, feeUsd: 0, ts: h.now() }] }));
    await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" });
    h.svc.reconcile([
      { instrument: WIN.upInstrument, qty: 8, avgCents: 42 },
      { instrument: WIN.downInstrument, qty: 5, avgCents: 57 },
    ]);
    const ps = h.svc.positions({ status: "open" });
    expect(ps.find((p) => p.side === "up")).toMatchObject({ qty: 8, costUsd: 3.36 });
    expect(ps.find((p) => p.side === "down")).toMatchObject({ qty: 5, avgCents: 57, mode: "live" });
    h.svc.reconcile([{ instrument: WIN.downInstrument, qty: 5, avgCents: 57 }]);
    expect(h.svc.positions().find((p) => p.side === "up")).toMatchObject({ status: "closed", qty: 0 });
  });
});

// ======================================================================= REST

describe("trading routes", () => {
  function server() {
    const h = setup();
    const app = buildServer(h.engine, h.db, { corsOrigin: "*" }, { register: (a) => h.mod.register(a), feedsIntervalMs: 60_000 });
    cleanup.unshift(() => app.close());
    return { ...h, app };
  }

  it("validates bodies and queries with ApiError codes", async () => {
    const { app } = server();
    const bad = await app.inject({ method: "POST", url: "/api/orders", payload: { side: "up", type: "limit", usd: 10, maxSlippageCents: 2 } });
    expect(bad.statusCode).toBe(400);
    expect(bad.json()).toMatchObject({ code: "validation", error: expect.stringContaining("limitCents") });
    expect((await app.inject({ method: "POST", url: "/api/orders", payload: { side: "sideways", type: "marketable", usd: 10, maxSlippageCents: 2 } })).statusCode).toBe(400);
    expect((await app.inject({ method: "POST", url: "/api/orders", payload: { side: "up", type: "marketable", usd: -1, maxSlippageCents: 2 } })).statusCode).toBe(400);
    expect((await app.inject({ method: "GET", url: "/api/orders?status=bogus" })).statusCode).toBe(400);
    expect((await app.inject({ method: "GET", url: "/api/positions?limit=0" })).statusCode).toBe(400);
    expect((await app.inject({ method: "DELETE", url: "/api/orders/missing" })).json()).toMatchObject({ code: "not_found" });
    expect((await app.inject({ method: "POST", url: "/api/positions/missing/close" })).statusCode).toBe(404);
  });

  it("place -> bracket -> list -> close round trip", async () => {
    const { app } = server();
    const r = await app.inject({
      method: "POST",
      url: "/api/orders",
      payload: { side: "up", type: "marketable", usd: 21, maxSlippageCents: 2, bracket: { tpCents: 60, slCents: null, btcStop: null, timeExitSec: null } },
    });
    expect(r.statusCode).toBe(200);
    const body = r.json();
    expect(body.order.status).toBe("filled");
    expect(body.bracket).toMatchObject({ tpCents: 60, status: "active" });
    const pid = body.position.id as string;

    const badBr = await app.inject({ method: "PUT", url: `/api/positions/${pid}/bracket`, payload: { tpCents: 30, slCents: 40 } });
    expect(badBr.statusCode).toBe(400);
    const br = await app.inject({ method: "PUT", url: `/api/positions/${pid}/bracket`, payload: { tpCents: 61, slCents: 35, btcStop: null, timeExitSec: null } });
    expect(br.json()).toMatchObject({ tpCents: 61, slCents: 35, id: body.bracket.id });
    expect((await app.inject({ method: "GET", url: "/api/brackets?status=active" })).json()).toHaveLength(1);
    expect((await app.inject({ method: "GET", url: "/api/positions?status=open" })).json()).toHaveLength(1);
    expect((await app.inject({ method: "GET", url: `/api/fills?windowId=${WIN.id}` })).json()).toHaveLength(1);
    expect((await app.inject({ method: "GET", url: "/api/orders?status=filled&limit=5" })).json()).toHaveLength(1);

    const c = await app.inject({ method: "POST", url: `/api/positions/${pid}/close`, payload: { maxSlippageCents: 1 } });
    expect(c.statusCode).toBe(200);
    expect(c.json().order).toMatchObject({ action: "sell", limitCents: 39, reason: "close" });
    expect(c.json().position.status).toBe("closed");
    const rm = await app.inject({ method: "PUT", url: `/api/positions/${pid}/bracket`, headers: { "content-type": "application/json" }, payload: "null" });
    expect(rm.statusCode).toBe(400); // position closed
  });

  it("snapshot carries recent orders / fills / positions / brackets", async () => {
    const h = setup();
    await openUp(h, { tpCents: 60, slCents: null, btcStop: null, timeExitSec: null });
    const s = h.mod.snapshot!();
    expect(s.orders).toHaveLength(1);
    expect(s.fills).toHaveLength(1);
    expect(s.positions![0]).toMatchObject({ status: "open", markCents: 40 });
    expect(s.brackets).toHaveLength(1);
    h.advance(25 * 3600_000);
    h.engine.win = null;
    const later = h.mod.snapshot!();
    expect(later.orders).toHaveLength(0);
    expect(later.positions).toHaveLength(1); // still open
  });
});

// ======================================================================= hardening (review findings)

/** live mode with a confirmed session and an executor that fills every order at `px` (sells: `sellPx`) */
function liveSetup(opts: { sell?: (o: Order) => ExecResult } = {}) {
  const h = setup();
  h.engine.settings.mode = "live";
  h.live.sessions.add("s1");
  const ex = new FakeLive((o) =>
    o.action === "sell" && opts.sell
      ? opts.sell(o)
      : { externalId: `x-${o.id}`, status: "filled", fills: [{ externalId: null, priceCents: o.action === "buy" ? 42 : 40, qty: o.action === "buy" ? 10 : o.qty, feeUsd: 0, ts: h.now() }] },
  );
  h.live.executor = ex;
  return { ...h, ex };
}

describe("kill / flatten", () => {
  it("a partial kill keeps retrying through a triggered bracket with a wider cap until flat", async () => {
    const h = setup();
    await openUp(h, null); // 50 @ 42, entry cap 2
    h.setBook(lv([40, 10], [30, 100]), lv([42, 100]));
    expect(await h.svc.flattenAll("kill")).toBe(1);
    let p = h.svc.positions({ status: "open" })[0]!;
    expect(p.qty).toBe(40);
    const b = h.svc.brackets({ status: "active" })[0]!;
    expect(b).toMatchObject({ positionId: p.id, status: "triggered", triggerReason: "kill", maxSlippageCents: 10 });
    expect(p.bracketId).toBe(b.id);
    expect(h.sent("notice").find((n) => n.title === "Kill switch: not flat yet")?.message).toMatch(/Up 40\.0/);

    h.core.evaluate(); // retry throttled for EXIT_RETRY_MS
    await h.core.idle();
    expect(sells(h)).toHaveLength(1);
    h.advance(EXIT_RETRY_MS);
    h.setBook(lv([33, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)[0]).toMatchObject({ reason: "kill", status: "filled", qty: 40, limitCents: 23 });
    p = h.svc.positions()[0]!;
    expect(p.status).toBe("closed");
    expect(h.svc.brackets()[0]).toMatchObject({ id: b.id, status: "done" });
  });

  it("a flatten that cannot trade at all (no bid) still arms the retry", async () => {
    const h = setup();
    await openUp(h, { tpCents: 90, slCents: null, btcStop: null, timeExitSec: null });
    h.setBook([], lv([42, 100]));
    expect(await h.svc.flattenAll("kill")).toBe(0);
    expect(h.svc.brackets()[0]).toMatchObject({ status: "triggered", triggerReason: "kill", tpCents: 90 });
    expect(h.sent("notice").filter((n) => n.title.includes("exit failed"))).toHaveLength(0); // reported once, by flatten
    h.advance(EXIT_RETRY_MS);
    h.setBook(lv([41, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(h.svc.positions()[0]!.status).toBe("closed");
  });

  it("refuses leaving live mode while a live position of the current window is open", async () => {
    const h = liveSetup();
    h.core.initMode();
    expect(h.core.modeSwitchBlocker({ mode: "paper" }, { mode: "live" })).toBeNull();
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" });
    expect(h.core.modeSwitchBlocker({ mode: "paper" }, { mode: "live" })).toMatch(/1 live position is still open/);
    expect(h.core.modeSwitchBlocker({ mode: "live" }, { mode: "live" })).toBeNull();
    await h.svc.closePosition(r.position!.id, { liveSession: "s1" });
    expect(h.core.modeSwitchBlocker({ mode: "paper" }, { mode: "live" })).toBeNull();
  });

  it("live positions after switching to paper: exits name the cause, kill reports them, retries once reachable", async () => {
    const h = liveSetup();
    h.core.initMode();
    const r = await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" });
    const ex = h.live.executor;
    h.engine.settings.mode = "paper";
    h.live.executor = null; // the live module hands out no executor in paper mode
    h.core.onSettings(h.engine.getSettings());
    expect(h.sent("notice").find((n) => n.title === "Live positions still open")).toMatchObject({ level: "error" });
    await expect(h.svc.closePosition(r.position!.id)).rejects.toMatchObject({ code: "live_unavailable", message: expect.stringContaining("LIVE position") });
    expect(await h.svc.flattenAll("kill")).toBe(0);
    expect(h.sent("notice").find((n) => n.title === "Kill switch: not flat yet")?.message).toMatch(/Up 10\.0 \(LIVE\).*executor is unavailable/);
    expect(h.svc.brackets()[0]).toMatchObject({ status: "triggered", triggerReason: "kill" });
    h.live.executor = ex;
    h.advance(EXIT_RETRY_MS);
    h.setBook(lv([40, 100]), lv([42, 100]));
    h.core.evaluate();
    await h.core.idle();
    expect(h.svc.positions()[0]).toMatchObject({ mode: "live", status: "closed" });
  });
});

describe("stuck exits", () => {
  it("exits the exchange keeps refusing back off instead of writing an order every second", async () => {
    const h = liveSetup({ sell: () => ({ externalId: null, status: "rejected", fills: [], error: "insufficient balance" }) });
    await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1", bracket: { tpCents: null, slCents: 41, btcStop: null, timeExitSec: null } });
    h.setBook(lv([41, 100]), lv([43, 100]));
    for (let i = 0; i < 20; i++) {
      h.core.evaluate();
      await h.core.idle();
      h.advance(1000);
      h.setBook(lv([41, 100]), lv([43, 100]));
    }
    // attempts at t = 0, 2, 6, 14 s (1s each without the backoff)
    expect(sells(h)).toHaveLength(4);
    expect(sells(h).every((o) => o.status === "rejected")).toBe(true);
    expect(h.sent("notice").filter((n) => n.title === "Stop loss exit failed")).toHaveLength(1);
    // a new bid retries sooner
    h.setBook(lv([40.5, 100]), lv([43, 100]));
    h.advance(5000);
    h.core.evaluate();
    await h.core.idle();
    expect(sells(h)).toHaveLength(5);
  });
});

describe("live reconcile", () => {
  it("books vanished contracts at the bid, reports old -> new quantities, never adopts at 0¢", async () => {
    const h = liveSetup();
    await h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" }); // 10 @ 42
    h.svc.reconcile([{ instrument: WIN.upInstrument, qty: 8, avgCents: 42 }]); // up bid 40
    expect(h.sent("notice").at(-1)!.message).toBe("Up: local 10.0 → exchange 8.0; 2.0 gone, booked -$0.04 at the 40¢ bid (estimate)");
    expect(h.svc.riskInputs(WIN.id).dailyRealizedPnlUsd).toBeCloseTo(-0.04, 9);
    h.svc.reconcile([]);
    const up = h.svc.positions().find((p) => p.side === "up")!;
    expect(up).toMatchObject({ status: "closed", qty: 0 });
    expect(up.realizedPnlUsd).toBeCloseTo(-0.2, 9);
    expect(h.svc.riskInputs(WIN.id).dailyRealizedPnlUsd).toBeCloseTo(-0.2, 9);
    expect(h.sent("notice").at(-1)!.message).toMatch(/^Up: local 8\.0 → exchange 0\.0/);

    // found without an average: priced at the mark (Down bid = 100 - Up ask 42), not 0
    h.svc.reconcile([{ instrument: WIN.downInstrument, qty: 5, avgCents: null }]);
    const down = h.svc.positions({ status: "open" }).find((p) => p.side === "down")!;
    expect(down).toMatchObject({ avgCents: 58, costUsd: 2.9, unrealizedPnlUsd: 0 });
  });

  it("does not adopt an unpriced exchange position, and skips a side with a working order", async () => {
    const h = liveSetup();
    h.engine.odds = null;
    h.engine.book = null;
    h.svc.reconcile([{ instrument: WIN.downInstrument, qty: 5, avgCents: null }]);
    h.svc.reconcile([{ instrument: WIN.downInstrument, qty: 5, avgCents: null }]);
    expect(h.svc.positions()).toHaveLength(0);
    expect(h.sent("notice").filter((n) => n.title === "Exchange position not adopted")).toHaveLength(1);

    h.setBook(lv([40, 100]), lv([42, 100]));
    h.live.executor = new FakeLive((o) => ({ externalId: `x-${o.id}`, status: "partial", fills: [{ externalId: null, priceCents: 40, qty: 4, feeUsd: 0, ts: h.now() }] }));
    await h.svc.placeOrder({ side: "up", type: "limit", usd: 4, limitCents: 40, maxSlippageCents: 2, liveSession: "s1" });
    h.svc.reconcile([]); // fills still arriving for the working Up order: leave the Up position alone
    expect(h.svc.positions({ status: "open" }).find((p) => p.side === "up")).toMatchObject({ qty: 4 });
  });
});

describe("ended windows", () => {
  it("picks up a missed settlement from the watcher; marks at the last bid meanwhile", async () => {
    const h = setup();
    await openUp(h, null);
    h.core.positions(); // marks at the 40 bid
    h.engine.win = null;
    h.setTime(WIN.endTs + 1000);
    expect(h.svc.positions({ status: "open" })[0]).toMatchObject({ markCents: 40 });
    h.core.settleEnded();
    expect(h.svc.positions()[0]!.status).toBe("open");
    h.db.upsertWindow({ ...WIN, status: "settled", result: "up", settlePrice: 67_100 }, h.now());
    h.core.settleEnded(); // throttled
    expect(h.svc.positions()[0]!.status).toBe("open");
    h.advance(10_000);
    h.core.settleEnded();
    expect(h.svc.positions()[0]).toMatchObject({ status: "settled", markCents: 100 });
  });

  it("closes positions of a window that never settles at cost, freeing the exposure", async () => {
    const h = setup();
    await openUp(h, { tpCents: 90, slCents: null, btcStop: null, timeExitSec: null });
    h.engine.win = null;
    h.setTime(WIN.endTs + 3600_000);
    h.core.settleEnded();
    expect(h.svc.riskInputs(null).openExposureUsd).toBe(21);
    h.setTime(WIN.endTs + 6 * 3600_000);
    h.core.settleEnded();
    const p = h.svc.positions()[0]!;
    expect(p).toMatchObject({ status: "closed", qty: 0, costUsd: 0, realizedPnlUsd: 0 });
    expect(h.svc.brackets()[0]!.status).toBe("cancelled");
    expect(h.svc.riskInputs(null)).toMatchObject({ openExposureUsd: 0, dailyRealizedPnlUsd: 0 });
    expect(h.sent("notice").find((n) => n.title === "Window never settled")?.message).toMatch(/Closed at cost/);
  });
});

describe("orphaned pending orders", () => {
  const orphan = (h: ReturnType<typeof setup>, p: Partial<Order>): Order => {
    const o: Order = {
      id: `orphan-${Math.random()}`,
      clientOrderId: null,
      externalId: null,
      mode: "paper",
      windowId: WIN.id,
      instrument: WIN.upInstrument,
      side: "up",
      action: "sell",
      type: "marketable",
      limitCents: 38,
      maxSlippageCents: 2,
      qty: 50,
      notionalUsd: null,
      filledQty: 0,
      avgFillCents: null,
      feesUsd: 0,
      status: "pending",
      reason: "close",
      bracketId: null,
      positionId: null,
      error: null,
      createdTs: h.now(),
      updatedTs: h.now(),
      ...p,
    };
    h.core.store.insertOrder(o);
    return o;
  };

  it("an orphaned pending sell does not block exits; recover() cancels it", async () => {
    const h = setup();
    const r = await openUp(h, null);
    const o = orphan(h, { positionId: r.position!.id });
    const c = await h.svc.closePosition(r.position!.id);
    expect(c.order).toMatchObject({ status: "filled", qty: 50 });
    h.core.recover();
    expect(h.svc.orders().find((x) => x.id === o.id)).toMatchObject({ status: "cancelled", error: "interrupted by a restart" });
    expect(h.sent("notice").some((n) => n.title === "Order interrupted")).toBe(true);
  });

  it("a pending buy with no submit in flight times out (no phantom exposure)", async () => {
    const h = setup();
    const o = orphan(h, { action: "buy", type: "limit", limitCents: 40, qty: 50, notionalUsd: 20, mode: "live" });
    h.engine.settings.mode = "live";
    expect(h.svc.riskInputs(WIN.id).openExposureUsd).toBe(20);
    h.advance(29_000);
    h.core.expireOrders();
    expect(h.svc.orders()[0]!.status).toBe("pending");
    h.advance(1_000);
    h.core.expireOrders();
    expect(h.svc.orders().find((x) => x.id === o.id)).toMatchObject({ status: "cancelled", error: expect.stringContaining("reconcile adopts any fill") });
    expect(h.svc.riskInputs(WIN.id).openExposureUsd).toBe(0);
  });

  it("a slow live submit is not mistaken for an orphan", async () => {
    const h = setup();
    h.engine.settings.mode = "live";
    h.live.sessions.add("s1");
    let finish!: (r: ExecResult) => void;
    h.live.executor = { mode: "live", submit: () => new Promise<ExecResult>((r) => (finish = r)), cancel: async () => {} };
    const p = h.svc.placeOrder({ side: "up", type: "marketable", usd: 4.2, maxSlippageCents: 2, liveSession: "s1" });
    await new Promise((r) => setTimeout(r, 0));
    h.advance(60_000);
    h.core.expireOrders();
    expect(h.svc.orders()[0]!.status).toBe("pending");
    finish({ externalId: "x1", status: "filled", fills: [{ externalId: null, priceCents: 42, qty: 10, feeUsd: 0, ts: h.now() }] });
    expect((await p).order.status).toBe("filled");
  });
});

describe("bracket validation", () => {
  it("refuses a TP at/below or an SL at/above the side's bid (it would trigger immediately)", async () => {
    const h = setup(); // Up bid 40
    const r = await openUp(h, null);
    const leg = (tpCents: number | null, slCents: number | null) => ({ tpCents, slCents, btcStop: null, timeExitSec: null });
    expect(() => h.svc.setBracket(r.position!.id, leg(40, null))).toThrow(/would trigger immediately/);
    expect(() => h.svc.setBracket(r.position!.id, leg(null, 40))).toThrow(/would trigger immediately/);
    expect(h.svc.setBracket(r.position!.id, leg(41, 39))).toMatchObject({ tpCents: 41, slCents: 39 });
  });

  it("time exit needs at least 3 s (0 / 1 could never fire before the close)", async () => {
    const h = setup();
    const r = await openUp(h, null);
    for (const t of [0, 1, 2.5]) {
      expect(() => h.svc.setBracket(r.position!.id, { tpCents: null, slCents: null, btcStop: null, timeExitSec: t })).toThrow(/at least 3 s/);
    }
    expect(h.svc.setBracket(r.position!.id, { tpCents: null, slCents: null, btcStop: null, timeExitSec: 3 })).toMatchObject({ timeExitSec: 3 });
  });
});
