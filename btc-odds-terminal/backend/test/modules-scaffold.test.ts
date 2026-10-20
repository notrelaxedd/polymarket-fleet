// Steps 3-6 scaffold: module wiring (one deps object, late-bound services, routes, snapshot parts),
// ApiError bodies, settings.risk persistence and the engine's Up book polling (flip for a short-side Up).
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type { BookLevels, BtcTick, FeedStatus, MarketWindow, OddsSnap, ServerMsg } from "../../shared/protocol.ts";
import { DEFAULT_RISK_LIMITS } from "../../shared/protocol.ts";
import { sideBook } from "../../shared/paper.ts";
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import { Engine, type BtcSource, type WindowSource } from "../src/engine.ts";
import { createModules } from "../src/modules/index.ts";
import type { NormalizedBook } from "../src/polymarket/index.ts";
import { Recorder } from "../src/recorder.ts";
import { buildServer } from "../src/server/index.ts";

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

class FakeTracker extends EventEmitter implements WindowSource {
  constructor(public cur: MarketWindow | null) {
    super();
  }
  start(): void {}
  stop(): void {}
  current(): MarketWindow | null {
    return this.cur;
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

const now = Date.now();
const win = (upInstrument: string): MarketWindow => ({
  id: "w1",
  title: "w1",
  startTs: now - 60_000,
  endTs: now + 600_000,
  priceToBeat: 67000,
  upInstrument,
  downInstrument: "x",
  status: "live",
  result: null,
  settlePrice: null,
});

const rawBook: NormalizedBook = {
  slug: "m",
  bids: [
    { price: 40, qty: 10 },
    { price: 39, qty: 5 },
  ],
  asks: [
    { price: 42, qty: 7 },
    { price: 43, qty: 0 },
  ],
  state: "MARKET_STATE_OPEN",
  transactTime: null,
};

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setup(upInstrument = "m:long") {
  const db = openDb(":memory:");
  const config = loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv);
  const logger = pino({ level: "silent" });
  const engine = new Engine({
    config,
    db,
    recorder: new Recorder(db),
    logger,
    btc: new FakeBtc(),
    tracker: new FakeTracker(win(upInstrument)),
    fetchBars: null,
    settlements: null,
    books: { getBook: async () => rawBook },
  });
  const msgs: ServerMsg[] = [];
  engine.on("msg", (m: ServerMsg) => msgs.push(m));
  const modules = createModules({ config, db, engine, logger });
  engine.extendSnapshot(() => modules.snapshot());
  const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => modules.register(a), feedsIntervalMs: 60_000 });
  engine.start();
  modules.start();
  cleanup.push(async () => {
    await app.close();
    await modules.stop();
    engine.stop();
    db.close();
  });
  return { db, engine, modules, app, msgs };
}

describe("steps 3-6 scaffold", () => {
  it("serves every module route and merges module snapshot parts", async () => {
    const { app, engine } = setup();
    for (const url of ["/api/orders", "/api/fills", "/api/positions", "/api/brackets", "/api/drawings", "/api/alerts", "/api/alerts/events"]) {
      const r = await app.inject({ method: "GET", url });
      expect(r.statusCode, url).toBe(200);
      expect(Array.isArray(r.json()), url).toBe(true);
    }
    const book = await app.inject({ method: "GET", url: "/api/book" });
    expect(book.statusCode).toBe(200);
    const risk = await app.inject({ method: "GET", url: "/api/risk" });
    expect(risk.json().limits).toEqual(DEFAULT_RISK_LIMITS);
    const an = await app.inject({ method: "GET", url: "/api/analysis?scope=last20" });
    expect(an.json().scope).toBe("last20");
    expect((await app.inject({ method: "GET", url: "/api/analysis?scope=nope" })).statusCode).toBe(400);
    expect((await app.inject({ method: "GET", url: "/api/replay/missing" })).json()).toMatchObject({ code: "not_found" });
    const snap = engine.snapshot();
    expect(snap.risk?.limits).toEqual(DEFAULT_RISK_LIMITS);
    expect(snap.live?.available).toBe(false);
    expect(snap.orders).toEqual([]);
    expect(snap.settings.liveAvailable).toBe(false);
  });

  it("returns ApiError bodies with a code (live confirm without keys)", async () => {
    const { app } = setup();
    const r = await app.inject({ method: "POST", url: "/api/live/confirm" });
    expect(r.statusCode).toBe(409);
    expect(r.json()).toMatchObject({ code: "live_unavailable" });
  });

  it("PUT /api/risk persists limits through settings.risk", async () => {
    const { app, engine, msgs } = setup();
    const r = await app.inject({ method: "PUT", url: "/api/risk", payload: { maxPositionUsd: 42 } });
    expect(r.statusCode).toBe(200);
    expect(r.json().limits.maxPositionUsd).toBe(42);
    expect(engine.getSettings().risk?.maxPositionUsd).toBe(42);
    expect(msgs.some((m) => m.type === "risk" && m.data.limits.maxPositionUsd === 42)).toBe(true);
    const bad = await app.inject({ method: "PUT", url: "/api/settings", payload: { risk: { maxPositionUsd: -1 } } });
    expect(bad.statusCode).toBe(400);
  });

  it("polls the Up book and flips it when Up is the short side", async () => {
    const long = setup("m:long");
    const b1 = (await long.engine.refreshBook()) as BookLevels;
    expect(b1.bids).toEqual([{ price: 40, size: 10 }, { price: 39, size: 5 }]);
    expect(b1.asks).toEqual([{ price: 42, size: 7 }]);
    expect(long.msgs.some((m) => m.type === "book")).toBe(true);
    expect(long.engine.snapshot().book?.windowId).toBe("w1");

    const short = setup("m:short");
    const b2 = (await short.engine.refreshBook()) as BookLevels;
    expect(b2.bids).toEqual([{ price: 58, size: 7 }]);
    expect(b2.asks).toEqual([{ price: 60, size: 10 }, { price: 61, size: 5 }]);
    // Down book derived from the Up book
    expect(sideBook(b1, "down").asks[0]).toEqual({ price: 60, size: 10 });
  });
});
