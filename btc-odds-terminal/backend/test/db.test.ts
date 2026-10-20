import { describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import type { Bar, MarketWindow } from "../../shared/protocol.ts";
import { DAY_MS, MIGRATIONS, migrate, openDb, SCHEMA_VERSION, TerminalDb } from "../src/db/index.ts";
import { loadSettings, saveSettings, SettingsPatchSchema } from "../src/db/settings.ts";
import { Recorder } from "../src/recorder.ts";

const T0 = Date.UTC(2026, 9, 20, 14, 0, 0);

const bar = (t: number, c: number, d = 1): Bar => ({ t, d, o: c - 1, h: c + 2, l: c - 2, c });

const win = (id: string, startTs: number, over: Partial<MarketWindow> = {}): MarketWindow => ({
  id,
  title: `BTC Up or Down ${id}`,
  startTs,
  endTs: startTs + 15 * 60_000,
  priceToBeat: null,
  upInstrument: `${id}:up`,
  downInstrument: `${id}:down`,
  status: "live",
  result: null,
  settlePrice: null,
  ...over,
});

describe("migrations", () => {
  it("creates every table and sets user_version", () => {
    const db = openDb(":memory:");
    expect(db.schemaVersion).toBe(SCHEMA_VERSION);
    expect(db.raw.pragma("user_version", { simple: true })).toBe(MIGRATIONS.length);
    const tables = (db.raw.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as { name: string }[]).map(
      (r) => r.name,
    );
    for (const t of [
      "bars", "ticks", "odds", "fair", "windows", "settings",
      "orders", "fills", "positions", "brackets", "drawings", "alerts", "alert_events", "risk_daily",
    ]) {
      expect(tables).toContain(t);
    }
    db.close();
  });

  it("is idempotent and refuses a newer schema", () => {
    const raw = new Database(":memory:");
    expect(migrate(raw)).toBe(SCHEMA_VERSION);
    expect(migrate(raw)).toBe(SCHEMA_VERSION);
    raw.pragma(`user_version = ${SCHEMA_VERSION + 1}`);
    expect(() => new TerminalDb(raw)).toThrow(/newer/);
  });
});

describe("repository round-trips", () => {
  it("ticks keep per-exchange sources", () => {
    const db = openDb(":memory:");
    db.insertTicks([
      { ts: T0, price: 100, sources: { coinbase: 99, kraken: 100, bitstamp: 101 } },
      { ts: T0 + 100, price: 101, sources: { coinbase: 101 } },
    ]);
    const ticks = db.ticks(T0, T0 + 1000);
    expect(ticks).toEqual([
      { ts: T0, price: 100, sources: { coinbase: 99, kraken: 100, bitstamp: 101 } },
      { ts: T0 + 100, price: 101, sources: { coinbase: 101 } },
    ]);
  });

  it("odds and fair round-trip with nulls, ascending, optional window filter", () => {
    const db = openDb(":memory:");
    db.insertOdds([
      { ts: T0 + 2000, windowId: "w1", upBid: 40, upAsk: 42, downBid: 58, downAsk: 60, upMid: 41 },
      { ts: T0 + 1000, windowId: "w1", upBid: null, upAsk: 42, downBid: null, downAsk: null, upMid: 42 },
      { ts: T0 + 3000, windowId: "w2", upBid: 1, upAsk: 3, downBid: 97, downAsk: 99, upMid: 2 },
    ]);
    db.insertFair([{ ts: T0 + 1000, windowId: "w1", fair: 55.5, sigma: 0.0001, tLeftSec: 600, edge: null }]);
    const odds = db.odds(T0, T0 + 10_000);
    expect(odds.map((o) => o.ts)).toEqual([T0 + 1000, T0 + 2000, T0 + 3000]);
    expect(odds[0]).toEqual({ ts: T0 + 1000, windowId: "w1", upBid: null, upAsk: 42, downBid: null, downAsk: null, upMid: 42 });
    expect(db.odds(T0, T0 + 10_000, "w2")).toHaveLength(1);
    expect(db.fair(T0, T0 + 10_000)).toEqual([
      { ts: T0 + 1000, windowId: "w1", fair: 55.5, sigma: 0.0001, tLeftSec: 600, edge: null },
    ]);
  });

  it("windows upsert never loses PTB/result and never un-settles", () => {
    const db = openDb(":memory:");
    db.upsertWindow(win("a", T0, { priceToBeat: 67000 }));
    db.upsertWindow(win("a", T0, { priceToBeat: null, status: "closed" }));
    expect(db.getWindow("a")).toMatchObject({ priceToBeat: 67000, status: "closed" });
    db.upsertWindow(win("a", T0, { status: "settled", result: "up", settlePrice: 67100 }));
    db.upsertWindow(win("a", T0, { status: "closed" }));
    expect(db.getWindow("a")).toMatchObject({ status: "settled", result: "up", settlePrice: 67100, priceToBeat: 67000 });
    expect(db.getWindow("missing")).toBeNull();
  });

  it("lists windows newest first and recent finished windows only", () => {
    const db = openDb(":memory:");
    for (let i = 0; i < 12; i++) {
      const start = T0 + i * 15 * 60_000;
      db.upsertWindow(win(`w${i}`, start, { status: i < 10 ? "settled" : i === 10 ? "live" : "upcoming", result: i < 10 ? "down" : null }));
    }
    expect(db.listWindows(3).map((w) => w.id)).toEqual(["w11", "w10", "w9"]);
    const now = T0 + 10 * 15 * 60_000 + 60_000; // inside w10
    const recent = db.recentFinishedWindows(8, now);
    expect(recent).toHaveLength(8);
    expect(recent[0]!.id).toBe("w9");
    expect(recent.every((w) => w.status === "settled")).toBe(true);
  });

  it("settings are JSON key/values; loadSettings: DB wins over env defaults", () => {
    const db = openDb(":memory:");
    db.setSetting("x", { a: [1, 2] });
    expect(db.getSetting("x")).toEqual({ a: [1, 2] });
    expect(db.getSetting("nope")).toBeUndefined();

    const defaults = { mode: "paper" as const, volLookbackMin: 30, feeCentsPerContract: null };
    expect(loadSettings(db, defaults)).toEqual(defaults);
    // env defaults are never persisted: a later .env change applies to keys the user never set
    expect(Object.keys(db.allSettings())).toEqual(["x"]);
    expect(loadSettings(db, { ...defaults, feeCentsPerContract: 2, volLookbackMin: 15 })).toEqual({
      mode: "paper",
      volLookbackMin: 15,
      feeCentsPerContract: 2,
    });
    saveSettings(db, { volLookbackMin: 45, feeCentsPerContract: 1.5 });
    expect(loadSettings(db, { ...defaults, volLookbackMin: 10 })).toEqual({ mode: "paper", volLookbackMin: 45, feeCentsPerContract: 1.5 });
    saveSettings(db, { feeCentsPerContract: null });
    expect(loadSettings(db, { ...defaults, feeCentsPerContract: 2 }).feeCentsPerContract).toBeNull();
    // corrupt stored value falls back to default
    db.setSetting("volLookbackMin", -5);
    expect(loadSettings(db, defaults).volLookbackMin).toBe(30);

    expect(SettingsPatchSchema.safeParse({ mode: "yolo" }).success).toBe(false);
    expect(SettingsPatchSchema.safeParse({ bogus: 1 }).success).toBe(false);
  });
});

describe("history range queries", () => {
  it("bars: ascending, inclusive bounds, live 1s bars replace overlapping seeded 60s bars", () => {
    const db = openDb(":memory:");
    const m0 = T0; // three seeded minutes
    expect(db.insertBarsIgnore([bar(m0, 100, 60), bar(m0 + 60_000, 101, 60), bar(m0 + 120_000, 102, 60)])).toBe(3);
    expect(db.insertBarsIgnore([bar(m0, 999, 60)])).toBe(0); // idempotent seed
    // live 1s bars inside the second minute
    db.upsertBars([bar(m0 + 61_000, 200), bar(m0 + 62_000, 201)]);
    db.upsertBars([bar(m0 + 62_000, 202)]); // replace
    const all = db.bars(m0, m0 + 180_000);
    expect(all.map((b) => [b.t - m0, b.d])).toEqual([
      [0, 60],
      [61_000, 1],
      [62_000, 1],
      [120_000, 60],
    ]);
    expect(all[0]!.c).toBe(100);
    expect(all[2]!.c).toBe(202);
    expect(db.bars(m0 + 61_000, m0 + 62_000)).toHaveLength(2);
    expect(db.bars(m0 + 1, m0 + 60_999)).toHaveLength(0);
    expect(db.countBars(60)).toBe(3);
    expect(db.countBars(1)).toBe(2);
  });

  it("nearestBar1s picks the bar whose close is nearest ts within tolerance", () => {
    const db = openDb(":memory:");
    db.upsertBars([bar(T0, 1), bar(T0 + 1000, 2), bar(T0 + 5000, 3)]);
    expect(db.nearestBar1s(T0 + 2000)!.c).toBe(2);
    expect(db.nearestBar1s(T0 + 1000)!.c).toBe(1);
    expect(db.nearestBar1s(T0 + 5900)!.c).toBe(3);
    expect(db.nearestBar1s(T0 + 60_000, 10_000)).toBeNull();
  });

  it("nearestBar(ts, 60) finds the seeded minute bar closing at ts", () => {
    const db = openDb(":memory:");
    db.insertBarsIgnore([bar(T0 - 120_000, 10, 60), bar(T0 - 60_000, 11, 60), bar(T0, 12, 60)]);
    db.upsertBars([bar(T0 - 1000, 99)]); // a 1s bar is not a 60s bar
    expect(db.nearestBar(T0, 60)!.c).toBe(11); // minute [T0-60s, T0) closes at T0
    expect(db.nearestBar(T0 + 4000, 60, 5000)!.c).toBe(11);
    expect(db.nearestBar(T0 + 30_000, 60, 5000)).toBeNull();
  });

  it("unsettledWindows lists ended windows without a result inside the horizon, oldest first", () => {
    const db = openDb(":memory:");
    const q = 15 * 60_000;
    db.upsertWindow(win("old", T0 - 40 * q)); // ended long ago
    db.upsertWindow(win("a", T0 - 3 * q, { status: "closed" }));
    db.upsertWindow(win("b", T0 - 2 * q));
    db.upsertWindow(win("done", T0 - 2 * q + 1, { status: "settled", result: "up" }));
    db.upsertWindow(win("cur", T0 - 60_000)); // still running
    expect(db.unsettledWindows(T0 - 6 * 3_600_000, T0).map((w) => w.id)).toEqual(["a", "b"]);
  });

  it("prune applies retention per table", () => {
    const db = openDb(":memory:");
    const now = T0;
    db.insertTicks([{ ts: now - 4 * DAY_MS, price: 1, sources: {} }, { ts: now - 2 * DAY_MS, price: 2, sources: {} }]);
    db.upsertBars([bar(now - 31 * DAY_MS, 1), bar(now - 4 * DAY_MS, 2)]);
    db.insertOdds([{ ts: now - 31 * DAY_MS, windowId: "w", upBid: 1, upAsk: 2, downBid: 98, downAsk: 99, upMid: 1.5 }]);
    db.insertFair([{ ts: now - 4 * DAY_MS, windowId: "w", fair: 50, sigma: 1e-4, tLeftSec: 1, edge: 0 }]);
    expect(db.prune(now)).toEqual({ ticks: 1, bars: 1, odds: 1, fair: 0 });
    expect(db.ticks(0, now)).toHaveLength(1);
  });
});

describe("Recorder", () => {
  it("buffers rows and writes them in one flush; dedupes window upserts", () => {
    const db = openDb(":memory:");
    const rec = new Recorder(db);
    for (let i = 0; i < 50; i++) rec.addTick({ ts: T0 + i * 100, price: 100 + i, sources: {} });
    rec.addBar(bar(T0, 100));
    rec.addOdds({ ts: T0, windowId: "a", upBid: 1, upAsk: 2, downBid: 98, downAsk: 99, upMid: 1.5 });
    rec.addFair({ ts: T0, windowId: "a", fair: 50, sigma: 1e-4, tLeftSec: 900, edge: 48.5 });
    rec.addWindow(win("a", T0));
    rec.addWindow(win("a", T0, { priceToBeat: 1234 }));
    expect(db.ticks(0, T0 + 10_000)).toHaveLength(0); // nothing written yet
    expect(rec.pending()).toBe(54);
    expect(rec.flush()).toBe(54);
    expect(rec.pending()).toBe(0);
    expect(db.ticks(0, T0 + 10_000)).toHaveLength(50);
    expect(db.getWindow("a")!.priceToBeat).toBe(1234);
    expect(rec.flush()).toBe(0);
  });

  it("flushes early when the buffer is large and on stop()", () => {
    const db = openDb(":memory:");
    const rec = new Recorder(db, { maxBuffered: 10 });
    for (let i = 0; i < 10; i++) rec.addTick({ ts: T0 + i, price: 1, sources: {} });
    expect(db.ticks(0, T0 + 100)).toHaveLength(10);
    rec.addTick({ ts: T0 + 50, price: 1, sources: {} });
    rec.start();
    rec.stop();
    expect(db.ticks(0, T0 + 100)).toHaveLength(11);
  });
});
