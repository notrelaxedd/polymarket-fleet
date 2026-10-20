// Replay (step 6): GET /api/replay/:windowId returns the window's bars (with lead-in), thinned ticks, odds, fair,
// per-second frames and my fills; 404 / 409 errors; frame + tick-thinning helpers.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type { Bar, BtcTick, FairSnap, Fill, MarketWindow, OddsSnap } from "../../shared/protocol.ts";
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import type { ModuleDeps, Services } from "../src/modules/types.ts";
import { REPLAY_LEAD_MS, createReplay, type ReplayPayload } from "../src/replay/index.ts";
import { FRAME_STALE_MS, buildFrames, thinTicks } from "../src/replay/frames.ts";
import { buildServer } from "../src/server/index.ts";

const MIN = 60_000;
const T0 = Date.UTC(2026, 9, 20, 14, 30, 0); // 10:30 ET

const win = (startTs: number, over: Partial<MarketWindow> = {}): MarketWindow => ({
  id: `btc-updown-15m-${startTs / 1000}`,
  title: "BTC Up or Down",
  startTs,
  endTs: startTs + 15 * MIN,
  priceToBeat: 67_000,
  upInstrument: "up",
  downInstrument: "down",
  status: "settled",
  result: "up",
  settlePrice: 67_050,
  ...over,
});

const bar = (t: number, c: number, d = 1): Bar => ({ t, d, o: c - 1, h: c + 2, l: c - 2, c });
const odds = (windowId: string, ts: number, bid: number, ask: number): OddsSnap => ({
  ts,
  windowId,
  upBid: bid,
  upAsk: ask,
  downBid: 100 - ask,
  downAsk: 100 - bid,
  upMid: (bid + ask) / 2,
});
const fair = (windowId: string, ts: number, f: number, edge: number | null = null): FairSnap => ({
  ts,
  windowId,
  fair: f,
  sigma: 0.0001,
  tLeftSec: 300,
  edge,
});
const tick = (ts: number, price: number): BtcTick => ({ ts, price, sources: { coinbase: price + 1, kraken: price - 1 } });
const fill = (windowId: string, ts: number, id = `f${ts}`): Fill => ({
  id,
  orderId: `o${ts}`,
  externalId: null,
  mode: "paper",
  windowId,
  instrument: "up",
  side: "up",
  action: "buy",
  priceCents: 55,
  qty: 10,
  feeUsd: null,
  slippageCents: 0,
  ts,
});

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setup(now = T0 + 20 * MIN) {
  let t = now;
  const db = openDb(":memory:");
  const fills: Fill[] = [];
  const fillQueries: { limit?: number; windowId?: string }[] = [];
  let current: MarketWindow | null = null;
  const services = {} as Services;
  const deps: ModuleDeps = {
    config: loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv),
    db,
    engine: { currentWindow: () => current } as never,
    broadcast: () => {},
    logger: pino({ level: "silent" }),
    now: () => t,
    services,
  };
  const mod = createReplay(deps);
  Object.assign(services, {
    replay: mod.service,
    trading: {
      fills: (q: { limit?: number; windowId?: string } = {}) => {
        fillQueries.push(q);
        return fills.filter((f) => !q.windowId || f.windowId === q.windowId).sort((a, b) => b.ts - a.ts);
      },
    },
  } as Partial<Services>);
  const hubEngine = Object.assign(new EventEmitter(), { snapshot: () => ({}), feeds: () => [] });
  const app = buildServer(hubEngine as never, db as never, { corsOrigin: "*" }, { register: (a) => mod.register(a) });
  cleanup.push(async () => {
    await app.close();
    db.close();
  });
  return {
    db,
    app,
    fills,
    fillQueries,
    setNow: (ms: number) => (t = ms),
    setCurrent: (w: MarketWindow | null) => (current = w),
    get: async (id: string) => app.inject({ method: "GET", url: `/api/replay/${encodeURIComponent(id)}` }),
  };
}

/** Record 1s bars + 4 ticks/s over [from, to) and per-second odds/fair inside the window. */
function record(db: ReturnType<typeof openDb>, w: MarketWindow, from: number, to: number) {
  const bars: Bar[] = [];
  const ticks: BtcTick[] = [];
  const os: OddsSnap[] = [];
  const fs: FairSnap[] = [];
  for (let ts = from; ts < to; ts += 1000) {
    const px = 67_000 + (ts - w.startTs) / 1000;
    bars.push(bar(ts, px));
    for (let k = 0; k < 10; k++) ticks.push(tick(ts + k * 100, px + k / 10)); // 10/s raw
    if (ts >= w.startTs && ts <= w.endTs) {
      os.push(odds(w.id, ts, 50, 52));
      fs.push(fair(w.id, ts, 55, 4));
    }
  }
  db.writeBatch({ bars, ticks, odds: os, fair: fs });
}

describe("GET /api/replay/:windowId", () => {
  it("returns the window with lead-in bars, odds, fair, frames, thinned ticks and its fills (ascending)", async () => {
    const s = setup();
    const w = win(T0);
    const other = win(T0 - 15 * MIN);
    s.db.upsertWindow(other, T0);
    s.db.upsertWindow(w, T0);
    record(s.db, w, T0 - 20 * MIN, w.endTs + 2 * MIN);
    // odds / fair of ANOTHER window inside the same time range must not leak in
    s.db.writeBatch({ odds: [odds(other.id, T0 + 5_500, 10, 12)], fair: [fair(other.id, T0 + 5_500, 11)] });
    s.fills.push(fill(w.id, T0 + 60_000), fill(w.id, T0 + 30_000), fill(other.id, T0 - MIN));

    const r = await s.get(w.id);
    expect(r.statusCode).toBe(200);
    const d = r.json() as ReplayPayload;
    expect(d.window).toEqual(w);
    // bars: lead-in from start - REPLAY_LEAD_MS through the window end, ascending
    expect(d.bars[0].t).toBe(T0 - REPLAY_LEAD_MS);
    expect(d.bars[d.bars.length - 1].t).toBe(w.endTs);
    expect(d.bars.every((b, i) => i === 0 || b.t > d.bars[i - 1].t)).toBe(true);
    // odds/fair: only this window, inside [start, end]
    expect(d.odds.length).toBe(901);
    expect(d.odds.every((o) => o.windowId === w.id && o.ts >= w.startTs && o.ts <= w.endTs)).toBe(true);
    expect(d.fair.every((f) => f.windowId === w.id)).toBe(true);
    // frames: one per second over the window
    expect(d.frames.length).toBe(901);
    expect(d.frames[0].ts).toBe(w.startTs);
    expect(d.frames[900].ts).toBe(w.endTs);
    expect(d.frames[10]).toMatchObject({ upBid: 50, upAsk: 52, upMid: 51, fair: 55, edge: 4 });
    // btc of a frame = close of the latest bar closed by then (bar [ts-1s, ts) )
    expect(d.frames[10].btc).toBe(67_000 + 9);
    // ticks thinned to <= 4/s, spanning the lead-in too
    expect(d.ticks.length).toBeGreaterThan(0);
    expect(d.ticks[0].ts).toBeGreaterThanOrEqual(T0 - REPLAY_LEAD_MS);
    const perSec = new Map<number, number>();
    for (const t of d.ticks) perSec.set(Math.floor(t.ts / 1000), (perSec.get(Math.floor(t.ts / 1000)) ?? 0) + 1);
    expect(Math.max(...perSec.values())).toBeLessThanOrEqual(4);
    expect(d.ticks[0].sources.coinbase).toBeDefined();
    // fills of this window only, ascending
    expect(d.fills.map((f) => f.ts)).toEqual([T0 + 30_000, T0 + 60_000]);
    expect(s.fillQueries.at(-1)).toMatchObject({ windowId: w.id });
  });

  it("replays a live window up to now and serves the engine's current window before it is in the DB", async () => {
    const s = setup(T0 + 5 * MIN);
    const w = win(T0, { status: "live", result: null, settlePrice: null });
    s.setCurrent(w);
    record(s.db, w, T0 - 2 * MIN, T0 + 5 * MIN);
    const d = (await s.get(w.id)).json() as ReplayPayload;
    expect(d.window.id).toBe(w.id);
    expect(d.frames[d.frames.length - 1].ts).toBe(T0 + 5 * MIN);
    expect(d.bars[d.bars.length - 1].t).toBeLessThanOrEqual(T0 + 5 * MIN);
    expect(d.odds[d.odds.length - 1].ts).toBeLessThanOrEqual(T0 + 5 * MIN);
  });

  it("returns empty series (not an error) for a recorded window with no data, e.g. past tick retention", async () => {
    const s = setup();
    const w = win(T0);
    s.db.upsertWindow(w, T0);
    const d = (await s.get(w.id)).json() as ReplayPayload;
    expect(d.bars).toEqual([]);
    expect(d.ticks).toEqual([]);
    expect(d.odds).toEqual([]);
    expect(d.frames.length).toBe(901);
    expect(d.frames.every((f) => f.btc == null && f.upMid == null && f.edge == null)).toBe(true);
  });

  it("404 not_found for an unknown window, 409 no_market for one that has not started", async () => {
    const s = setup(T0);
    const r = await s.get("nope");
    expect(r.statusCode).toBe(404);
    expect(r.json()).toMatchObject({ code: "not_found" });
    const up = win(T0 + 15 * MIN, { status: "upcoming", result: null, settlePrice: null });
    s.db.upsertWindow(up, T0);
    const r2 = await s.get(up.id);
    expect(r2.statusCode).toBe(409);
    expect(r2.json()).toMatchObject({ code: "no_market" });
  });

  it("service.load returns null for unknown windows", () => {
    const s = setup();
    const mod = createReplay({
      db: s.db,
      engine: { currentWindow: () => null },
      now: () => T0,
      services: {},
    } as never);
    expect(mod.service.load("missing")).toBeNull();
  });
});

describe("buildFrames", () => {
  const W = "w";
  it("uses only bars closed by the frame time and the latest quote at or before it", () => {
    const bars = [bar(T0 - 1000, 100), bar(T0, 101), bar(T0 + 1000, 102)];
    const os = [odds(W, T0 + 500, 40, 44), odds(W, T0 + 2000, 60, 62)];
    const fs = [fair(W, T0 + 1000, 50)];
    const f = buildFrames(bars, os, fs, T0, T0 + 2000);
    expect(f.map((x) => x.ts)).toEqual([T0, T0 + 1000, T0 + 2000]);
    expect(f.map((x) => x.btc)).toEqual([100, 101, 102]);
    expect(f.map((x) => x.upMid)).toEqual([null, 42, 61]);
    expect(f.map((x) => x.fair)).toEqual([null, 50, 50]);
    expect(f.map((x) => x.edge)).toEqual([null, 8, -11]);
  });

  it("handles 60s seeded bars (closed only after their minute) and rounds the range to whole seconds", () => {
    const bars = [bar(T0 - 60_000, 90, 60), bar(T0, 95, 60)];
    const f = buildFrames(bars, [], [], T0 - 500, T0 + 59_400);
    expect(f[0].ts).toBe(T0);
    expect(f[f.length - 1].ts).toBe(T0 + 59_000);
    expect(f.every((x) => x.btc === 90)).toBe(true);
    expect(buildFrames(bars, [], [], T0, T0 + 60_000).at(-1)!.btc).toBe(95);
  });

  it("drops quotes and fair values older than FRAME_STALE_MS (feed gaps)", () => {
    const os = [odds(W, T0, 40, 44)];
    const fs = [fair(W, T0, 50)];
    const f = buildFrames([], os, fs, T0, T0 + FRAME_STALE_MS + 1000);
    expect(f[FRAME_STALE_MS / 1000].upMid).toBe(42);
    expect(f[FRAME_STALE_MS / 1000 + 1]).toMatchObject({ upMid: null, upBid: null, fair: null, edge: null });
  });

  it("returns no frames for an empty range", () => {
    expect(buildFrames([], [], [], T0 + 1000, T0)).toEqual([]);
  });
});

describe("thinTicks", () => {
  it("keeps the last tick of each 250ms slot", () => {
    const ticks = [tick(T0, 1), tick(T0 + 100, 2), tick(T0 + 249, 3), tick(T0 + 250, 4), tick(T0 + 900, 5), tick(T0 + 999, 6)];
    expect(thinTicks(ticks).map((t) => t.price)).toEqual([3, 4, 6]);
    expect(thinTicks([])).toEqual([]);
    expect(thinTicks(ticks, 1000).map((t) => t.price)).toEqual([6]);
  });
});
