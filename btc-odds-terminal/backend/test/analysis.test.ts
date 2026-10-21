// Pair-relationship analysis: lag (cross-correlation with a known lag), sensitivity (market regression vs
// analytic fair), scatter z / clipping / fair curve, takeaway sentences, and the /api/analysis module caches.
import { EventEmitter } from "node:events";
import Fastify from "fastify";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type { Bar, FairSnap, MarketWindow, OddsSnap } from "../../shared/protocol.ts";
import { LAG_STEPS_SEC } from "../../shared/analysis.ts";
import {
  MAX_MID_SPREAD_CENTS,
  NOT_ENOUGH,
  NO_LIVE_WINDOW,
  ODDS_MAX_AGE_SEC,
  SENS_MIN_TLEFT_SEC,
  SENS_WINDOW_SEC,
  Z_CLIP,
  assembleAnalysis,
  buildSeries,
  computeAnalysis,
  fairCentsPer10,
  fairCurve,
  lagBars,
  pearson,
  phi,
  sensitivitySeries,
  sensitivityShift,
  sensitivityTakeaway,
  validUpMid,
  windowPartial,
  type WindowData,
} from "../src/analysis/compute.ts";
import { createAnalysis, SCOPE_TTL_MS } from "../src/analysis/index.ts";
import { loadConfig } from "../src/config.ts";
import { openDb } from "../src/db/index.ts";
import { fairCents, normCdf } from "../src/fair/fairValue.ts";
import type { EngineApi, ModuleDeps, Services } from "../src/modules/types.ts";

// ---------------------------------------------------------------- synthetic market

function rng(seed: number) {
  let a = seed >>> 0;
  const u = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return () => Math.sqrt(-2 * Math.log(u() || 1e-12)) * Math.cos(2 * Math.PI * u());
}

const K = 67_000;
const SIGMA = 1e-4; // per sqrt(second): ~$6.7 per 1s
const T0 = Date.UTC(2026, 9, 20, 14, 0, 0);
const WIN_MS = 900_000;

const mkWindow = (i: number, ptb: number | null = K): MarketWindow => ({
  id: `btc-w${i}`,
  title: `w${i}`,
  startTs: T0 + i * WIN_MS,
  endTs: T0 + (i + 1) * WIN_MS,
  priceToBeat: ptb,
  upInstrument: `btc-w${i}:long`,
  downInstrument: `btc-w${i}:short`,
  status: "settled",
  result: "up",
  settlePrice: null,
});

interface SynthOpts {
  /** odds respond to BTC this many seconds later */
  lagSec: number;
  /** cents of Up per $ of BTC */
  beta?: number;
  /** odds noise, cents (sd) */
  noise?: number;
  seed?: number;
  /** seconds of data (default the whole window) */
  seconds?: number;
  /** BTC level relative to K at the start */
  s0?: number;
}

/** One window of 1s bars / odds / fair where Up mid = 50 + beta * (BTC(t - lag) - K) (+ noise). */
function synth(w: MarketWindow, o: SynthOpts): WindowData {
  const g = rng(o.seed ?? 7);
  const beta = o.beta ?? 0.05;
  const n = o.seconds ?? WIN_MS / 1000;
  const S: number[] = [];
  let s = o.s0 ?? K;
  for (let k = 0; k < n; k++) {
    if (k > 0) s *= Math.exp(SIGMA * g());
    S.push(s);
  }
  const bars: Bar[] = [];
  const odds: OddsSnap[] = [];
  const fair: FairSnap[] = [];
  for (let k = 0; k < n; k++) {
    const t = w.startTs + k * 1000;
    bars.push({ t, d: 1, o: S[k]!, h: S[k]!, l: S[k]!, c: S[k]! });
    const lagged = S[Math.max(0, k - o.lagSec)]!;
    const mid = 50 + beta * (lagged - K) + (o.noise ?? 0) * g();
    odds.push({ ts: t + 300, windowId: w.id, upBid: mid - 0.5, upAsk: mid + 0.5, downBid: null, downAsk: null, upMid: mid });
    const tLeftSec = (w.endTs - t - 500) / 1000;
    const f = fairCents(S[k]!, K, tLeftSec, SIGMA);
    fair.push({ ts: t + 500, windowId: w.id, fair: f, sigma: SIGMA, tLeftSec, edge: f - mid });
  }
  return { window: w, bars, odds, fair };
}

const series = (d: WindowData, until = d.window.endTs) => buildSeries(d, until)!;

// ---------------------------------------------------------------- lag

describe("lag cross-correlation", () => {
  it.each([
    [0, 0],
    [2, 2],
    [4, 4],
    [5, 4], // a 2s step at L covers delays L and L+1
    [10, 10],
    [24, 24],
  ])("finds a known %is lag at the %is bar", (lagSec, expected) => {
    const s = series(synth(mkWindow(0), { lagSec, noise: 0.05, seed: 11 + lagSec }));
    const { lag, lagPeak } = lagBars([windowPartial(s)]);
    expect(lag.map((b) => b.lagSec)).toEqual(LAG_STEPS_SEC);
    expect(lagPeak?.lagSec).toBe(expected);
    expect(lagPeak!.rho).toBeGreaterThan(0.5);
    // bars well away from the peak are near zero (BTC returns are independent)
    for (const b of lag) if (Math.abs(b.lagSec - expected) >= 4) expect(Math.abs(b.rho!)).toBeLessThan(0.15);
  });

  it("pools windows without pairing seconds across a window boundary", () => {
    const a = series(synth(mkWindow(0), { lagSec: 6, noise: 0.05, seed: 1 }));
    const b = series(synth(mkWindow(1), { lagSec: 6, noise: 0.05, seed: 2 }));
    const one = lagBars([windowPartial(a)]);
    const both = lagBars([windowPartial(a), windowPartial(b)]);
    expect(both.lagPeak?.lagSec).toBe(6);
    // per window: k = 1 .. n-L-2 -> n - L - 2 pairs
    for (const bar of both.lag) expect(bar.n).toBe(2 * (900 - bar.lagSec - 2));
    expect(one.lag[0]!.n).toBe(898);
  });

  it("is negative when odds move against BTC and null without data / variance", () => {
    const d = synth(mkWindow(0), { lagSec: 2, beta: -0.05, noise: 0.05 });
    const { lag, lagPeak } = lagBars([windowPartial(series(d))]);
    expect(lag[1]!.rho!).toBeLessThan(-0.5);
    expect(lagPeak == null || lagPeak.rho < 0.15).toBe(true);

    const flat = synth(mkWindow(0), { lagSec: 0, beta: 0 });
    const r = lagBars([windowPartial(series(flat))]);
    expect(r.lag.every((x) => x.rho === null)).toBe(true);
    expect(r.lagPeak).toBeNull();

    expect(lagBars([]).lag.every((x) => x.rho === null && x.n === 0)).toBe(true);
    expect(pearson(10, 1, 1, 1, 1, 1)).toBeNull();
  });

  it("tolerates gaps (forward fill is bounded) and missing odds", () => {
    const d = synth(mkWindow(0), { lagSec: 4, noise: 0.05, seed: 5 });
    // drop a 60s hole in BTC and odds
    const hole = (t: number) => t >= d.window.startTs + 300_000 && t < d.window.startTs + 360_000;
    d.bars = d.bars.filter((b) => !hole(b.t));
    d.odds = d.odds.filter((o) => !hole(o.ts));
    const s = series(d);
    expect(Number.isNaN(s.btc[330]!)).toBe(true);
    expect(s.btc[302]).toBe(s.btc[299]); // filled up to BTC_FILL_SEC
    const { lagPeak, lag } = lagBars([windowPartial(s)]);
    expect(lagPeak?.lagSec).toBe(4);
    expect(lag[0]!.n).toBeLessThan(898);
  });
});

// ---------------------------------------------------------------- sensitivity

describe("sensitivity (cents of Up per $10 BTC)", () => {
  it("recovers the market slope by regression (no lag)", () => {
    const beta = 0.05;
    const s = series(synth(mkWindow(0), { lagSec: 0, beta }));
    const pts = sensitivitySeries(s, 0);
    const withMarket = pts.filter((p) => p.market != null);
    expect(withMarket.length).toBeGreaterThan(pts.length * 0.8);
    for (const p of withMarket) expect(p.market!).toBeCloseTo(10 * beta, 3);
    // points every 5s, none inside the last SENS_MIN_TLEFT_SEC (dFair/dS ~ 1/sqrt(t) blows up there)
    expect(pts[1]!.ts - pts[0]!.ts).toBe(5000);
    expect(pts.at(-1)!.ts).toBe(mkWindow(0).endTs - SENS_MIN_TLEFT_SEC * 1000);
    expect(pts.every((p) => p.tLeftSec >= SENS_MIN_TLEFT_SEC)).toBe(true);
  });

  it("uses the measured lag to align BTC and odds changes", () => {
    const beta = 0.05;
    const d = synth(mkWindow(0), { lagSec: 4, beta, noise: 0.02, seed: 9 });
    const s = series(d);
    const { lagPeak } = lagBars([windowPartial(s)]);
    const shift = sensitivityShift(lagPeak);
    expect(shift).toBe(4);
    const aligned = sensitivitySeries(s, shift).filter((p) => p.market != null);
    const mean = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
    expect(mean(aligned.map((p) => p.market!))).toBeCloseTo(10 * beta, 2);
    // without the shift a 4s lag over 5s changes hides most of the response
    const naive = sensitivitySeries(s, 0).filter((p) => p.market != null);
    expect(mean(naive.map((p) => p.market!))).toBeLessThan(10 * beta * 0.4);
    expect(sensitivityShift({ lagSec: 4, rho: 0.05 })).toBe(0);
    expect(sensitivityShift({ lagSec: 30, rho: 0.9 })).toBe(10);
  });

  it("fair = analytic dFair/dS x $10 (matches a numeric derivative of the fair model)", () => {
    for (const [S, t] of [
      [67_000, 600],
      [67_150, 300],
      [66_900, 60],
      [67_020, 5],
    ] as const) {
      const analytic = fairCentsPer10(S, K, SIGMA, t)!;
      const numeric = (fairCents(S + 0.05, K, t, SIGMA) - fairCents(S - 0.05, K, t, SIGMA)) * 100;
      expect(Math.abs(analytic - numeric) / numeric).toBeLessThan(1e-5);
    }
    expect(fairCentsPer10(67_000, K, SIGMA, 0)).toBeNull();
    const s = series(synth(mkWindow(0), { lagSec: 0 }));
    // the point's fair = mean of the end-of-second derivative over the same trailing regression window
    const p = sensitivitySeries(s, 0)[40]!;
    const k = (p.ts - mkWindow(0).startTs) / 1000;
    let sum = 0;
    for (let j = k - SENS_WINDOW_SEC + 1; j <= k; j++) sum += fairCentsPer10(s.btc[j]!, K, SIGMA, p.tLeftSec + k - j - 1)!;
    expect(p.fair!).toBeCloseTo(sum / SENS_WINDOW_SEC, 3);
  });

  it("market is null until the rolling window has enough BTC movement", () => {
    const s = series(synth(mkWindow(0), { lagSec: 0, seconds: 20 }), mkWindow(0).startTs + 19_000);
    expect(sensitivitySeries(s, 0).every((p) => p.market === null)).toBe(true);
    const flatBtc = synth(mkWindow(0), { lagSec: 0 });
    flatBtc.bars = flatBtc.bars.map((b) => ({ ...b, c: K }));
    expect(sensitivitySeries(series(flatBtc), 0).every((p) => p.market === null)).toBe(true);
    expect(sensitivitySeries(null)).toEqual([]);
  });

  it("generates the sensitivity takeaway from the latest values", () => {
    const pt = (market: number | null, fair: number | null) => [{ ts: 0, tLeftSec: 100, market, fair }];
    expect(sensitivityTakeaway(pt(0.8, 1.3))).toBe("The market under-reacts: 0.8¢ per $10 BTC vs 1.3¢ fair (38% less).");
    expect(sensitivityTakeaway(pt(2.1, 1.3))).toBe("The market over-reacts: 2.1¢ per $10 BTC vs 1.3¢ fair (62% more).");
    expect(sensitivityTakeaway(pt(1.2, 1.3))).toBe("The market moves in line with fair: 1.2¢ vs 1.3¢ per $10 BTC.");
    expect(sensitivityTakeaway(pt(0.02, 0.01))).toMatch(/^Odds are pinned/);
    expect(sensitivityTakeaway(pt(null, 1.3))).toMatch(/^Fair moves 1.3¢ per \$10 BTC/);
    expect(sensitivityTakeaway([])).toBe(NOT_ENOUGH);
  });
});

// ---------------------------------------------------------------- scatter + assembly

describe("scatter, current point and takeaways", () => {
  it("z = ln(S/K)/(sigma sqrt t), clipped to +-2, one sample per 2s", () => {
    const d = synth(mkWindow(0), { lagSec: 0 });
    const p = windowPartial(series(d));
    expect(p.scatter.len).toBe(450); // even seconds 0..898 (the last one still has 1s left at its close)
    const i = 100;
    const ts = p.scatter.ts[i]!;
    const k = (ts - d.window.startTs) / 1000;
    // btc / mid are end-of-second values: time left is measured from the end of the bucket
    const tLeft = (d.window.endTs - ts) / 1000 - 1;
    expect(p.scatter.z[i]!).toBeCloseTo(Math.log(d.bars[k]!.c / K) / (SIGMA * Math.sqrt(tLeft)), 4);
    expect(p.scatter.tLeft[i]).toBe(tLeft);

    const far = windowPartial(series(synth(mkWindow(0), { lagSec: 0, s0: K * 1.05 })));
    expect(Array.from(far.scatter.z.subarray(0, far.scatter.len)).every((z) => z === Z_CLIP)).toBe(true);
    // no PTB -> no scatter (lag still counts)
    const noK = windowPartial(series(synth(mkWindow(0, null), { lagSec: 0, noise: 0.05 })));
    expect(noK.scatter.len).toBe(0);
    expect(noK.lag[0]).toBeGreaterThan(0);
  });

  it("fair curve is 100*phi(z) on -2..2 and phi matches the fair model's normCdf", () => {
    const c = fairCurve();
    expect(c).toHaveLength(41);
    expect(c[0]!.z).toBe(-2);
    expect(c[20]).toEqual({ z: 0, fair: 50 });
    expect(c[40]!.fair).toBeCloseTo(97.725, 3);
    for (const x of [-6, -2.5, -1, -0.1, 0, 0.3, 1.7, 8]) expect(phi(x)).toBeCloseTo(normCdf(x), 12);
  });

  it("writes the scatter takeaway at the live point (\"At +1.3σ the market sits 2¢ under fair.\")", () => {
    const w = mkWindow(0);
    const now = w.endTs - 400_000;
    const S = K * Math.exp(1.3 * SIGMA * Math.sqrt(400));
    const fair = 100 * phi(1.3);
    const d = synth(w, { lagSec: 4, noise: 0.05, seconds: 500 });
    const s = buildSeries(d, now)!;
    const out = assembleAnalysis({
      scope: "window",
      now,
      windows: [w],
      partials: [windowPartial(s)],
      current: w,
      currentSeries: s,
      live: { ts: now - 200, btc: S, upMid: fair - 2, sigma: SIGMA, fair },
    });
    expect(out.current).toMatchObject({ z: 1.3, tLeftSec: 400 });
    expect(out.current!.fair).toBeCloseTo(fair, 2);
    expect(out.takeaways.scatter).toBe("At +1.3σ the market sits 2¢ under fair.");
    expect(out.takeaways.lag).toMatch(/^Odds trail BTC by ~4s \(peak ρ 0\.\d\d over this window\)\.$/);
    expect(out.lagPeak?.lagSec).toBe(4);
    expect(out.windowIds).toEqual([w.id]);
    expect(out.sensitivity.length).toBeGreaterThan(50);
    expect(out.sensitivity.at(-1)!.ts).toBeLessThanOrEqual(now);

    // stale live point: falls back to the recorded series (most recent second)
    const fallback = assembleAnalysis({ scope: "window", now, windows: [w], partials: [], current: w, currentSeries: s, live: null });
    expect(fallback.current).not.toBeNull();
    expect(fallback.current!.upMid).toBeCloseTo(s.mid[s.n - 1]!, 2);
    // "Beyond" wording and the "under/over" side
    const beyond = assembleAnalysis({
      scope: "window",
      now,
      windows: [w],
      partials: [],
      current: w,
      currentSeries: s,
      live: { ts: now, btc: K * Math.exp(-3 * SIGMA * 20), upMid: 3, sigma: SIGMA, fair: 0.13 },
    });
    expect(beyond.current!.z).toBe(-2);
    expect(beyond.takeaways.scatter).toBe("Beyond −2σ the market sits 2.9¢ over fair.");
  });

  it("without a live point the scatter takeaway summarizes the scope", () => {
    const wins = [mkWindow(0), mkWindow(1)];
    const data = wins.map((w, i) => synth(w, { lagSec: 2, noise: 0.05, seed: 30 + i, s0: K * 1.004 }));
    // market 3¢ under fair everywhere above the strike
    for (const d of data) {
      const S = new Map(d.bars.map((b) => [b.t, b.c]));
      d.odds = d.odds.map((o) => {
        const t = o.ts - 300;
        const f = fairCents(S.get(t)!, K, (d.window.endTs - t) / 1000, SIGMA);
        return { ...o, upMid: f > 50 ? f - 3 : f };
      });
    }
    const out = computeAnalysis({
      scope: "last20",
      now: wins[1]!.endTs + 60_000,
      windows: wins,
      bars: data.flatMap((d) => d.bars),
      odds: data.flatMap((d) => d.odds),
      fair: data.flatMap((d) => d.fair),
      current: null,
    });
    expect(out.windowIds).toEqual(wins.map((w) => w.id));
    expect(out.current).toBeNull();
    expect(out.sensitivity).toEqual([]);
    expect(out.takeaways.scatter).toMatch(/^Across the last 2 windows, the market averages 3¢ under fair above the strike/);
    expect(out.scatter.length).toBe(2 * 450);
    expect(out.scatter[0]!.windowId).toBe(wins[0]!.id);
  });

  it("downsamples the scatter to the scope's max and says when there is no data", () => {
    const wins = Array.from({ length: 8 }, (_, i) => mkWindow(i));
    const data = wins.map((w, i) => synth(w, { lagSec: 0, noise: 0.05, seed: i }));
    const out = computeAnalysis({
      scope: "all",
      now: wins.at(-1)!.endTs + 1,
      windows: wins,
      bars: data.flatMap((d) => d.bars),
      odds: data.flatMap((d) => d.odds),
      fair: data.flatMap((d) => d.fair),
      current: null,
    });
    expect(out.scatter.length).toBeLessThanOrEqual(2500);
    expect(out.scatter.length).toBeGreaterThan(1500);
    for (let i = 1; i < out.scatter.length; i++) expect(out.scatter[i]!.ts).toBeGreaterThan(out.scatter[i - 1]!.ts);
    expect(out.takeaways.lag).toMatch(/no measurable lag over all 8 windows/);

    const empty = computeAnalysis({ scope: "window", now: T0, windows: [], bars: [], odds: [], fair: [], current: null });
    expect(empty.takeaways).toEqual({ scatter: NOT_ENOUGH, lag: NOT_ENOUGH, sensitivity: NOT_ENOUGH });
    expect(empty.lag).toHaveLength(16);
    expect(empty.fairCurve).toHaveLength(41);
  });
});

// ---------------------------------------------------------------- module (/api/analysis)

class FakeEngine extends EventEmitter {
  constructor(public cur: MarketWindow | null) {
    super();
  }
  currentWindow() {
    return this.cur;
  }
  latestTick() {
    return null;
  }
  latestOdds() {
    return null;
  }
  latestFair() {
    return null;
  }
}

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setupModule() {
  const db = openDb(":memory:");
  const prev = mkWindow(0);
  const cur: MarketWindow = { ...mkWindow(1), status: "live", result: null };
  const clock = { now: cur.startTs + 500_000 };
  const a = synth(prev, { lagSec: 6, noise: 0.05, seed: 101 });
  const b = synth(cur, { lagSec: 6, noise: 0.05, seed: 102, seconds: 500 });
  db.writeBatch({ windows: [prev, cur], bars: [...a.bars, ...b.bars], odds: [...a.odds, ...b.odds], fair: [...a.fair, ...b.fair] });
  const engine = new FakeEngine(cur);
  const deps: ModuleDeps = {
    config: loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv),
    db,
    engine: engine as unknown as EngineApi,
    broadcast: () => {},
    logger: pino({ level: "silent" }),
    now: () => clock.now,
    services: {} as Services,
  };
  const mod = createAnalysis(deps);
  const app = Fastify();
  mod.register(app);
  mod.start?.();
  cleanup.push(async () => {
    await app.close();
    await mod.stop?.();
    db.close();
  });
  const get = async (scope: string) => {
    const r = await app.inject({ method: "GET", url: `/api/analysis?scope=${scope}` });
    return { status: r.statusCode, body: r.json() };
  };
  return { db, engine, clock, mod, app, get, prev, cur };
}

describe("GET /api/analysis", () => {
  it("computes each scope from the recorded rows", async () => {
    const { get, prev, cur } = setupModule();
    const w = await get("window");
    expect(w.status).toBe(200);
    expect(w.body.windowIds).toEqual([cur.id]);
    expect(w.body.lagPeak.lagSec).toBe(6);
    expect(w.body.current).not.toBeNull();
    expect(w.body.sensitivity.length).toBeGreaterThan(90);
    expect(w.body.sensitivity.at(-1).market).toBeCloseTo(0.5, 1);

    const l = await get("last20");
    expect(l.body.windowIds).toEqual([prev.id, cur.id]);
    // the live series runs to the second of `now` (501 buckets, the last forward-filled)
    expect(l.body.lag[3].n).toBe(900 - 6 - 2 + (501 - 6 - 2));
    const all = await get("all");
    expect(all.body.windowIds).toEqual([prev.id, cur.id]);
    expect((await get("nope")).status).toBe(400);
    expect((await get("")).status).toBe(400);
  });

  it("caches payloads per scope (short TTL) and finished windows indefinitely", async () => {
    const { get, clock, db, engine, prev, mod } = setupModule();
    const first = await get("all");
    clock.now += 1_000;
    expect((await get("all")).body.computedTs).toBe(first.body.computedTs);
    expect(mod.service.compute("all").computedTs).toBe(first.body.computedTs);

    // the finished window's rows vanish: its cached partial still counts, the live window recomputes
    db.raw.prepare("DELETE FROM odds WHERE window_id = ?").run(prev.id);
    clock.now += SCOPE_TTL_MS.all;
    const later = await get("all");
    expect(later.body.computedTs).toBe(clock.now);
    const liveOnly = await get("window");
    expect(later.body.lag[0].n - liveOnly.body.lag[0].n).toBe(898);

    // a rollover drops the payload cache immediately
    const win1 = await get("window");
    clock.now += 500;
    engine.emit("rollover", {});
    expect((await get("window")).body.computedTs).not.toBe(win1.body.computedTs);
  });

  it("falls back to the last finished window when nothing is live", async () => {
    const { get, engine, clock, prev, cur } = setupModule();
    engine.cur = null;
    clock.now = cur.startTs + 10_000;
    // cur is not finished in the db (end > now) -> the last finished window is prev
    const r = await get("window");
    expect(r.body.windowIds).toEqual([prev.id]);
    expect(r.body.current).toBeNull();
    expect(r.body.sensitivity).toEqual([]);
    expect(r.body.lagPeak.lagSec).toBe(6);
    // the takeaways say it is the last window, not "this window"
    expect(r.body.takeaways.lag).toMatch(/over the last window\)\.$/);
    expect(r.body.takeaways.lag).not.toMatch(/this window/);
    expect(r.body.takeaways.sensitivity).toBe(NO_LIVE_WINDOW);
  });

  it("last20 is 20 windows in total, the live one included", async () => {
    const { db, get, cur, engine } = setupModule();
    // 25 more finished windows before prev (index 0): w-25 .. w-1
    const older = Array.from({ length: 25 }, (_, i) => mkWindow(i - 25));
    const data = older.map((w, i) => synth(w, { lagSec: 2, noise: 0.05, seed: 200 + i, seconds: 120 }));
    db.writeBatch({
      windows: older,
      bars: data.flatMap((d) => d.bars),
      odds: data.flatMap((d) => d.odds),
      fair: data.flatMap((d) => d.fair),
    });
    const live = await get("last20");
    expect(live.body.windowIds).toHaveLength(20);
    expect(live.body.windowIds.at(-1)).toBe(cur.id);
    expect(live.body.takeaways.lag).toMatch(/the last 20 windows/);
    // nothing live: the 20 most recent finished windows
    engine.cur = null;
    engine.emit("rollover", {});
    const fin = await get("last20");
    expect(fin.body.windowIds).toHaveLength(20);
    expect(fin.body.windowIds).not.toContain(cur.id);
  });
});

// ---------------------------------------------------------------- data quality (one-sided books, stalled feed)

describe("mid validity and stale odds", () => {
  it("validUpMid needs both sides, an uncrossed book and a spread under the cap", () => {
    expect(validUpMid({ upBid: 49, upAsk: 51, upMid: 50 })).toBe(50);
    expect(validUpMid({ upBid: 49, upAsk: 51, upMid: null })).toBe(50);
    expect(validUpMid({ upBid: 3, upAsk: null, upMid: 3 })).toBeNull();
    expect(validUpMid({ upBid: null, upAsk: 97, upMid: 97 })).toBeNull();
    expect(validUpMid({ upBid: 52, upAsk: 50, upMid: 51 })).toBeNull();
    expect(validUpMid({ upBid: 40, upAsk: 40 + MAX_MID_SPREAD_CENTS + 1, upMid: 50.5 })).toBeNull();
    expect(validUpMid(null)).toBeNull();
  });

  it("one-sided odds rows never enter the grid, the scatter residuals or the lag sums", () => {
    const clean = synth(mkWindow(0), { lagSec: 2, noise: 0.05, seed: 77 });
    const dirty = synth(mkWindow(0), { lagSec: 2, noise: 0.05, seed: 77 });
    // every 37th second the ask vanishes and the recorded mid collapses to the bid (3¢)
    dirty.odds = dirty.odds.map((o, i) => (i % 37 === 5 ? { ...o, upBid: 3, upAsk: null, upMid: 3 } : o));
    const s = series(dirty);
    expect(Math.min(...Array.from(s.mid).filter((x) => !Number.isNaN(x)))).toBeGreaterThan(20);
    expect(s.midAge[5]).toBe(1); // forward-filled from second 4
    const pc = windowPartial(series(clean));
    const pd = windowPartial(s);
    const resid = (p: typeof pc) => Math.max(...Array.from(p.scatter.resid.subarray(0, p.scatter.len)).map(Math.abs));
    expect(resid(pd)).toBeLessThan(resid(pc) + 5);
    const lc = lagBars([pc]);
    const ld = lagBars([pd]);
    expect(ld.lagPeak?.lagSec).toBe(2);
    expect(ld.lagPeak!.rho).toBeGreaterThan(lc.lagPeak!.rho - 0.05);
  });

  it("a stalled odds feed adds no zero-change pairs to the lag or the sensitivity regression", () => {
    const beta = 0.05;
    const d = synth(mkWindow(0), { lagSec: 0, beta, seed: 13 });
    // the odds feed stalls 5s out of every 20s (within ODDS_FILL_SEC, so the grid forward-fills it)
    d.odds = d.odds.filter((o) => Math.floor((o.ts - d.window.startTs) / 1000) % 20 < 15);
    const s = series(d);
    expect(s.midAge[18]).toBe(4);
    expect(Number.isNaN(s.mid[18]!)).toBe(false);
    // the regression only sees (nearly) real odds changes: far less attenuated than with every frozen second in
    const mean = (xs: number[]) => xs.reduce((x, y) => x + y, 0) / xs.length;
    const slope = (ser: typeof s) => mean(sensitivitySeries(ser, 0).flatMap((p) => (p.market == null ? [] : [p.market])));
    const unfiltered = { ...s, midAge: new Float64Array(s.n) };
    expect(sensitivitySeries(s, 0).filter((p) => p.market != null).length).toBeGreaterThan(50);
    expect(Math.abs(slope(s) - 10 * beta)).toBeLessThan(0.5 * Math.abs(slope(unfiltered) - 10 * beta));
    // lag pairs: only those with both odds endpoints at most ODDS_MAX_AGE_SEC old
    const { lag } = lagBars([windowPartial(s)]);
    let expected = 0;
    for (let k = 1; k + 1 < s.n; k++) if (s.midAge[k + 1]! <= ODDS_MAX_AGE_SEC && s.midAge[k - 1]! <= ODDS_MAX_AGE_SEC) expected++;
    expect(lag[0]!.n).toBe(expected);
    expect(lag[0]!.n).toBeLessThan(898 * 0.9);
    // a stalled mid is not a scatter sample either
    const p = windowPartial(s);
    for (let i = 0; i < p.scatter.len; i++) {
      const k = (p.scatter.ts[i]! - d.window.startTs) / 1000;
      expect(s.midAge[k]!).toBeLessThanOrEqual(ODDS_MAX_AGE_SEC);
    }
  });

  it("scope 'window' without a live window labels the takeaways as the last window", () => {
    const w = mkWindow(0);
    const d = synth(w, { lagSec: 2, noise: 0.05, seed: 3 });
    const out = computeAnalysis({ scope: "window", now: w.endTs + 60_000, windows: [w], bars: d.bars, odds: d.odds, fair: d.fair, current: null });
    expect(out.takeaways.lag).toMatch(/over the last window\)\.$/);
    expect(out.takeaways.sensitivity).toBe(NO_LIVE_WINDOW);
    const liveOut = computeAnalysis({ scope: "window", now: w.endTs - 1_000, windows: [w], bars: d.bars, odds: d.odds, fair: d.fair, current: w });
    expect(liveOut.takeaways.lag).toMatch(/this window/);
  });
});
