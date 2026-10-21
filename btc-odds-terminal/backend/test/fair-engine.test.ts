// Engine wiring: fair value per window, rollover, PTB switch, stale-input suppression, vol warm-up,
// settings, recording cadence, PTB fallback and settlement back-fill. Fake feeds + fake clock.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Bar, BtcTick, FairSnap, FeedStatus, MarketWindow, OddsSnap, ServerMsg } from "../../shared/protocol.ts";
import { loadConfig } from "../src/config.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import { Engine, type BtcSource, type EngineDeps, type SettlementSource, type WindowSource } from "../src/engine.ts";
import { Recorder } from "../src/recorder.ts";

const Q = 15 * 60_000;
const T0 = Date.UTC(2026, 9, 20, 14, 0, 0); // a window boundary

class FakeBtc extends EventEmitter implements BtcSource {
  start(): void {}
  stop(): void {}
  status(): FeedStatus[] {
    return [];
  }
  last(): BtcTick | null {
    return null;
  }
  tick(price: number, ts = Date.now()): void {
    this.emit("tick", { ts, price, sources: { coinbase: price } } satisfies BtcTick);
  }
}

class FakeTracker extends EventEmitter implements WindowSource {
  cur: MarketWindow | null = null;
  nxt: MarketWindow | null = null;
  odds: OddsSnap | null = null;
  start(): void {}
  stop(): void {}
  current(): MarketWindow | null {
    return this.cur;
  }
  next(): MarketWindow | null {
    return this.nxt;
  }
  lastOdds(): OddsSnap | null {
    return this.odds;
  }
  status(): FeedStatus {
    return { id: "polymarket", state: "live", lastMsgTs: null, ageMs: null };
  }
  quote(windowId: string, upMid: number, ts = Date.now()): OddsSnap {
    const o: OddsSnap = { ts, windowId, upBid: upMid - 1, upAsk: upMid + 1, downBid: 99 - upMid, downAsk: 101 - upMid, upMid };
    this.odds = o;
    this.emit("odds", o);
    return o;
  }
}

const win = (id: string, startTs: number, ptb: number | null, over: Partial<MarketWindow> = {}): MarketWindow => ({
  id,
  title: id,
  startTs,
  endTs: startTs + Q,
  priceToBeat: ptb,
  upInstrument: `${id}-up:long`,
  downInstrument: `${id}-up:short`,
  status: "live",
  result: null,
  settlePrice: null,
  ...over,
});

/** `minutes` seeded 60s bars ending at `end`, alternating log returns of +/-r. */
function minuteBars(end: number, minutes: number, r: number, p0 = 100_000): Bar[] {
  const out: Bar[] = [];
  let p = p0;
  for (let i = minutes; i >= 1; i--) {
    p *= Math.exp(i % 2 === 0 ? r : -r);
    out.push({ t: end - i * 60_000, d: 60, o: p, h: p, l: p, c: p });
  }
  return out;
}

let engine: Engine | null = null;

function setup(opts: { seed?: Bar[]; deps?: Partial<EngineDeps>; db?: TerminalDb } = {}) {
  const db = opts.db ?? openDb(":memory:");
  if (opts.seed) db.insertBarsIgnore(opts.seed);
  const btc = new FakeBtc();
  const tracker = new FakeTracker();
  const recorder = new Recorder(db);
  const config = loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv);
  const e = new Engine({
    config,
    db,
    recorder,
    logger: pino({ level: "silent" }),
    btc,
    tracker,
    fetchBars: null,
    settlements: null,
    ...opts.deps,
  });
  engine = e;
  const msgs: ServerMsg[] = [];
  e.on("msg", (m: ServerMsg) => msgs.push(m));
  const fairs = () => msgs.filter((m): m is Extract<ServerMsg, { type: "fair" }> => m.type === "fair").map((m) => m.data);
  const lastFair = (): FairSnap | undefined => fairs().at(-1);
  return { db, btc, tracker, recorder, engine: e, msgs, fairs, lastFair };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0 + 5 * 60_000); // 5 min into window w1
});

afterEach(() => {
  engine?.stop();
  engine = null;
  vi.useRealTimers();
});

const warmSeed = () => minuteBars(T0 + 5 * 60_000, 40, 0.001);

describe("Engine fair value", () => {
  it("prices the current window (K, tLeft), and after a rollover the new one; odds/fair reset", () => {
    const { btc, tracker, engine: e, lastFair } = setup({ seed: warmSeed() });
    const w1 = win("w1", T0, 100_000);
    const w2 = win("w2", T0 + Q, 100_200);
    tracker.cur = w1;
    tracker.nxt = w2;
    e.start();

    btc.tick(100_100);
    let f = lastFair()!;
    expect(f.windowId).toBe("w1");
    expect(f.tLeftSec).toBeCloseTo(600, 3);
    expect(f.fair).toBeGreaterThan(50);
    expect(f.edge).toBeNull();
    expect(f.sigma).toBeCloseTo(0.001 / Math.sqrt(60), 6);

    vi.advanceTimersByTime(300);
    tracker.quote("w1", 55);
    f = lastFair()!;
    expect(f.edge).toBeCloseTo(f.fair - 55, 9);
    expect(e.snapshot().lastOdds?.windowId).toBe("w1");

    // rollover at 14:15
    vi.setSystemTime(T0 + Q + 100);
    tracker.cur = w2;
    tracker.nxt = win("w3", T0 + 2 * Q, null);
    tracker.odds = null;
    tracker.emit("rollover", { current: w2, next: tracker.nxt });
    const snap = e.snapshot();
    expect(snap.lastOdds).toBeNull(); // never the settled window's book
    expect(snap.lastFair === null || snap.lastFair.windowId === "w2").toBe(true);

    vi.advanceTimersByTime(300);
    btc.tick(100_100);
    f = lastFair()!;
    expect(f.windowId).toBe("w2");
    expect(f.tLeftSec).toBeGreaterThan(899);
    expect(f.fair).toBeLessThan(50); // below the new K
    expect(f.edge).toBeNull();
  });

  it("starts pricing when the PTB arrives mid-window and follows a PTB change", () => {
    const { btc, tracker, engine: e, fairs, lastFair } = setup({ seed: warmSeed() });
    tracker.cur = win("w1", T0, null);
    e.start();
    btc.tick(100_000);
    vi.advanceTimersByTime(2000);
    expect(fairs()).toHaveLength(0);

    tracker.cur = { ...tracker.cur, priceToBeat: 99_000 };
    tracker.emit("window", tracker.cur);
    btc.tick(100_000);
    vi.advanceTimersByTime(300); // fair recomputes are throttled to FAIR_MIN_GAP_MS
    expect(lastFair()!.fair).toBeGreaterThan(90);

    tracker.cur = { ...tracker.cur, priceToBeat: 101_000 }; // metadata replaces a proxy fallback
    tracker.emit("window", tracker.cur);
    vi.advanceTimersByTime(300);
    expect(lastFair()!.fair).toBeLessThan(10);
  });

  it("publishes nothing while realized vol covers less than the minimum span", () => {
    const { btc, tracker, engine: e, fairs } = setup();
    tracker.cur = win("w1", T0, 100_000);
    e.start();
    let p = 100_000;
    for (let i = 0; i < 200; i++) {
      p *= Math.exp(i % 2 ? 2e-4 : -2e-4);
      btc.tick(p);
      vi.advanceTimersByTime(1000);
    }
    expect(fairs()).toHaveLength(0); // 200 s of 1s returns < 300 s
    for (let i = 0; i < 120; i++) {
      p *= Math.exp(i % 2 ? 2e-4 : -2e-4);
      btc.tick(p);
      vi.advanceTimersByTime(1000);
    }
    expect(fairs().length).toBeGreaterThan(0);
    expect(fairs()[0]!.sigma).toBeCloseTo(2e-4, 5);
  });

  it("suppresses fair on a stale BTC tick and edge on a stale Polymarket quote", () => {
    const { btc, tracker, engine: e, fairs, lastFair } = setup({ seed: warmSeed() });
    tracker.cur = win("w1", T0, 100_000);
    e.start();
    btc.tick(100_050);
    tracker.quote("w1", 60);
    vi.advanceTimersByTime(1000);
    expect(lastFair()!.edge).not.toBeNull();

    // BTC quiet past STALE_AFTER_MS: the 1 Hz clock stops producing fair
    vi.advanceTimersByTime(2500);
    const n = fairs().length;
    vi.advanceTimersByTime(3000);
    expect(fairs()).toHaveLength(n);

    // BTC fresh again, but the quote is ~6.5 s old: edge = null (not fair minus a frozen mid)
    btc.tick(100_050);
    expect(fairs().length).toBeGreaterThan(n);
    expect(lastFair()!.edge).toBeNull();
    tracker.quote("w1", 60);
    vi.advanceTimersByTime(300);
    expect(lastFair()!.edge).not.toBeNull();
  });

  it("a longer vol lookback rebuilds sigma from the DB; only the changed key is persisted", () => {
    const end = T0 + 5 * 60_000;
    const seed = [...minuteBars(end - 30 * 60_000, 30, 0.004), ...minuteBars(end, 30, 0.001)];
    const { btc, tracker, engine: e, db, lastFair } = setup({ seed });
    tracker.cur = win("w1", T0, 100_000);
    e.start();
    btc.tick(100_000);
    const s30 = lastFair()!.sigma;
    expect(s30).toBeLessThan(0.0015 / Math.sqrt(60));

    vi.advanceTimersByTime(300);
    expect(e.updateSettings({ volLookbackMin: 60 }).volLookbackMin).toBe(60);
    const s60 = lastFair()!.sigma;
    expect(s60).toBeGreaterThan(2 * s30);
    expect(db.allSettings()).toEqual({ volLookbackMin: 60 });
  });

  it("records odds once per 1 s bucket even when 1 Hz polls jitter around 1000 ms", () => {
    const { tracker, engine: e, db, recorder } = setup({ seed: warmSeed() });
    tracker.cur = win("w1", T0, 100_000);
    e.start();
    const base = T0 + 5 * 60_000 + 500;
    const gaps = [995, 1008, 990, 1003, 997, 1010, 992, 1001, 999];
    let ts = base;
    tracker.quote("w1", 50, ts);
    for (const g of gaps) tracker.quote("w1", 50, (ts += g));
    recorder.flush();
    expect(db.odds(base - 1, ts + 1)).toHaveLength(gaps.length + 1);
  });
});

describe("Engine PTB fallback, settings and back-fills", () => {
  it("priceAt falls back to the 60s bar closing at the window start", () => {
    const { engine: e } = setup({ seed: minuteBars(T0 + 60_000, 10, 0.001) });
    const bars = minuteBars(T0 + 60_000, 10, 0.001);
    expect(e.priceAt(T0)).toBe(bars.find((b) => b.t === T0 - 60_000)!.c);
    expect(e.priceAt(T0 - 3 * 3_600_000)).toBeNull();
  });

  it("a stored live mode without API keys starts in paper", () => {
    const db = openDb(":memory:");
    db.setSetting("mode", "live");
    const { engine: e } = setup({ db });
    expect(e.getSettings().mode).toBe("paper");
  });

  it("start-up 60s back-fill warms vol so fair is available immediately", async () => {
    const fetchBars = vi.fn(async () => minuteBars(T0 + 5 * 60_000, 60, 0.001));
    const { btc, tracker, engine: e, db, lastFair } = setup({ deps: { fetchBars } });
    tracker.cur = win("w1", T0, 100_000);
    e.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchBars).toHaveBeenCalledTimes(1);
    expect(db.countBars(60)).toBe(60);
    btc.tick(100_000);
    expect(lastFair()?.windowId).toBe("w1");
  });

  it("settles windows that ended while the worker was down", async () => {
    const db = openDb(":memory:");
    db.upsertWindow(win("old", T0 - 4 * Q, 99_000, { status: "closed" }));
    db.upsertWindow(win("zero", T0 - 3 * Q, 99_000, { status: "live" }));
    db.upsertWindow(win("done", T0 - 2 * Q, 99_000, { status: "settled", result: "down" }));
    const settlements: SettlementSource = {
      getSettlement: vi.fn(async (slug: string) => ({ slug, value: slug === "old-up" ? 1 : 0 })),
      getBook: vi.fn(async () => ({ slug: "zero-up", state: "MARKET_STATE_OPEN", bids: [], offers: [] }) as never),
    };
    const { engine: e, msgs, recorder } = setup({ db, deps: { settlements } });
    e.start();
    await vi.advanceTimersByTimeAsync(0);
    recorder.flush();
    expect(db.getWindow("old")).toMatchObject({ result: "up", status: "settled", priceToBeat: 99_000 });
    expect(db.getWindow("zero")!.result).toBeNull(); // 0 while the book is still open: not trusted
    expect(msgs.some((m) => m.type === "window" && m.data.id === "old" && m.data.result === "up")).toBe(true);
    expect(settlements.getSettlement).not.toHaveBeenCalledWith("done-up");

    // the book closes: next retry settles it as down
    (settlements.getBook as ReturnType<typeof vi.fn>).mockResolvedValue({ slug: "zero-up", state: "MARKET_STATE_RESOLVED" });
    await vi.advanceTimersByTimeAsync(30_000);
    recorder.flush();
    expect(db.getWindow("zero")).toMatchObject({ result: "down", status: "settled" });
  });
});
