import { describe, it, expect } from "vitest";
import {
  normCdf,
  fairUp,
  fairCents,
  edgeCents,
  RealizedVol,
} from "../src/fair/index.js";

/** Seeded PRNG (mulberry32). */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Standard normal via Box-Muller. */
function gauss(rng: () => number): number {
  let u = 0;
  while (u === 0) u = rng();
  const v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** Driftless GBM closes at 1-second spacing, per-√s vol sigma. Returns n+1 prices. */
function gbmCloses(rng: () => number, n: number, sigma: number, p0 = 100000): number[] {
  const out = [p0];
  let x = Math.log(p0);
  for (let i = 0; i < n; i++) {
    x += sigma * gauss(rng);
    out.push(Math.exp(x));
  }
  return out;
}

describe("normCdf", () => {
  it("known values", () => {
    expect(normCdf(0)).toBe(0.5);
    expect(normCdf(1.96)).toBeCloseTo(0.9750021048517795, 10);
    expect(normCdf(-1)).toBeCloseTo(0.15865525393145707, 10);
    expect(normCdf(3)).toBeCloseTo(0.9986501019683699, 10);
    expect(normCdf(0.5)).toBeCloseTo(0.6914624612740131, 10);
    expect(normCdf(2.2)).toBeCloseTo(0.9860965524865014, 10);
  });

  it("symmetry: Φ(x) + Φ(-x) = 1", () => {
    for (const x of [0.1, 0.7, 1.3, 2.5, 4.2, 6.9, 7.5, 12]) {
      expect(normCdf(x) + normCdf(-x)).toBeCloseTo(1, 12);
    }
  });

  it("tails saturate and stay monotonic", () => {
    expect(normCdf(-40)).toBe(0);
    expect(normCdf(40)).toBe(1);
    expect(Math.abs(normCdf(-8) - 6.220960574271819e-16)).toBeLessThan(1e-20);
    let prev = -1;
    for (let x = -6; x <= 6; x += 0.05) {
      const p = normCdf(x);
      expect(p).toBeGreaterThan(prev);
      prev = p;
    }
  });

  it("NaN propagates", () => {
    expect(normCdf(NaN)).toBeNaN();
  });
});

describe("fairUp", () => {
  it("at-the-money is 0.5", () => {
    expect(fairUp(100000, 100000, 300, 0.0001)).toBe(0.5);
    expect(fairUp(100000, 100000, 0, 0.0001)).toBe(0.5);
  });

  it("matches the hand-computed case S=101000 K=100000 t=300 sigma=0.0001", () => {
    const d = Math.log(101000 / 100000) / (0.0001 * Math.sqrt(300));
    expect(fairUp(101000, 100000, 300, 0.0001)).toBeCloseTo(normCdf(d), 12);
    // Reference value from an independent erfc computation.
    expect(fairUp(101000, 100000, 300, 0.0001)).toBeCloseTo(0.9999999953992275, 9);
  });

  it("matches a non-saturated hand-computed case S=101000 K=100000 t=300 sigma=0.001", () => {
    expect(fairUp(101000, 100000, 300, 0.001)).toBeCloseTo(0.717179370757048, 9);
  });

  it("is monotonic increasing in S", () => {
    let prev = -1;
    for (const S of [99000, 99500, 99900, 100000, 100100, 100500, 101000, 102000]) {
      const p = fairUp(S, 100000, 300, 0.0002);
      expect(p).toBeGreaterThan(prev - 1e-15);
      prev = p;
    }
    expect(fairUp(99000, 100000, 300, 0.0002)).toBeLessThan(0.5);
    expect(fairUp(101000, 100000, 300, 0.0002)).toBeGreaterThan(0.5);
  });

  it("approaches a step function as t -> 0", () => {
    expect(fairUp(100100, 100000, 1e-6, 1e-4)).toBeCloseTo(1, 10);
    expect(fairUp(99900, 100000, 1e-6, 1e-4)).toBeCloseTo(0, 10);
    const t = [10, 1, 0.1, 0.01];
    const vals = t.map((tt) => fairUp(100050, 100000, tt, 1e-4));
    for (let i = 1; i < vals.length; i++) expect(vals[i]!).toBeGreaterThanOrEqual(vals[i - 1]!);
    expect(vals[vals.length - 1]).toBe(1);
  });

  it("edge cases: tSec <= 0 or sigma <= 0 is a step", () => {
    for (const [t, s] of [
      [0, 0.0001],
      [-5, 0.0001],
      [300, 0],
      [300, -1],
    ] as const) {
      expect(fairUp(100001, 100000, t, s)).toBe(1);
      expect(fairUp(99999, 100000, t, s)).toBe(0);
      expect(fairUp(100000, 100000, t, s)).toBe(0.5);
    }
  });

  it("edge cases: invalid S, K or non-finite inputs return NaN", () => {
    expect(fairUp(0, 100000, 300, 0.0001)).toBeNaN();
    expect(fairUp(-1, 100000, 300, 0.0001)).toBeNaN();
    expect(fairUp(100000, 0, 300, 0.0001)).toBeNaN();
    expect(fairUp(100000, -1, 300, 0.0001)).toBeNaN();
    expect(fairUp(NaN, 100000, 300, 0.0001)).toBeNaN();
    expect(fairUp(Infinity, 100000, 300, 0.0001)).toBeNaN();
    expect(fairUp(100000, 100000, NaN, 0.0001)).toBeNaN();
    expect(fairUp(100000, 100000, 300, NaN)).toBeNaN();
    expect(fairUp(100000, 100000, 300, Infinity)).toBeNaN();
  });

  it("output is always within [0, 1]", () => {
    const rng = mulberry32(7);
    for (let i = 0; i < 2000; i++) {
      const S = 1000 + rng() * 200000;
      const K = 1000 + rng() * 200000;
      const t = rng() * 900;
      const s = rng() * 0.01;
      const p = fairUp(S, K, t, s);
      expect(p).toBeGreaterThanOrEqual(0);
      expect(p).toBeLessThanOrEqual(1);
    }
  });
});

describe("fairCents / edgeCents", () => {
  it("fairCents is 100 * fairUp", () => {
    expect(fairCents(101000, 100000, 300, 0.001)).toBeCloseTo(71.7179370757048, 9);
    expect(fairCents(100000, 100000, 300, 0.001)).toBeCloseTo(50, 12);
  });

  it("edgeCents = fair - upMid, null when no midpoint", () => {
    expect(edgeCents(60, 55)).toBe(5);
    expect(edgeCents(40, 55)).toBe(-15);
    expect(edgeCents(60, null)).toBeNull();
    expect(edgeCents(60, NaN)).toBeNull();
    expect(edgeCents(NaN, 55)).toBeNull();
  });
});

describe("RealizedVol", () => {
  it("rejects invalid construction parameters", () => {
    expect(() => new RealizedVol(0)).toThrow(RangeError);
    expect(() => new RealizedVol(60, 0)).toThrow(RangeError);
    expect(() => new RealizedVol(60, 1, 0)).toThrow(RangeError);
  });

  it("returns null before minSamples (default 30)", () => {
    const rv = new RealizedVol(3600);
    // Pushes at buckets 0..30: 29 closed returns, one short of 30.
    for (let i = 0; i <= 30; i++) rv.push(i * 1000, 100000 + i);
    expect(rv.sigma()).toBeNull();
    expect(rv.count()).toBe(29);
    // One more bucket closes the 30th return.
    rv.push(31 * 1000, 100031);
    expect(rv.count()).toBe(30);
    expect(rv.sigma()).not.toBeNull();
  });

  it("recovers a known sigma from synthetic GBM (10 ticks/s, last price per second)", () => {
    const sigma = 5e-4;
    const n = 3600;
    const rng = mulberry32(42);
    const closes = gbmCloses(rng, n, sigma);
    const rv = new RealizedVol(n + 10);
    for (let i = 0; i < closes.length; i++) {
      for (let k = 0; k < 10; k++) {
        // Intra-second noise on all but the final tick; the final tick is the bucket close.
        const price = k < 9 ? closes[i]! * (1 + 0.01 * gauss(rng)) : closes[i]!;
        rv.push(i * 1000 + k * 100, price);
      }
    }
    const est = rv.sigma();
    expect(est).not.toBeNull();
    expect(Math.abs(est! - sigma) / sigma).toBeLessThan(0.1);
  });

  it("recovers sigma across gaps (random dropped seconds) without bias", () => {
    const sigma = 5e-4;
    const rng = mulberry32(99);
    const closes = gbmCloses(rng, 6000, sigma);
    const rv = new RealizedVol(10000);
    for (let i = 0; i < closes.length; i++) {
      if (rng() < 0.5) continue; // drop about half the seconds, creating gaps
      rv.push(i * 1000, closes[i]!);
    }
    const est = rv.sigma();
    expect(est).not.toBeNull();
    expect(Math.abs(est! - sigma) / sigma).toBeLessThan(0.1);
  });

  it("gap return is normalised by its length in samples", () => {
    const rv = new RealizedVol(3600, 1, 1);
    rv.push(0, 100);
    rv.push(10_000, 110); // closes bucket 0
    rv.push(11_000, 111); // closes bucket 10: return ln(1.1) over a 10-sample gap
    expect(rv.count()).toBe(1);
    expect(rv.sigma()).toBeCloseTo(Math.abs(Math.log(1.1)) / Math.sqrt(10), 12);
  });

  it("consecutive returns give sqrt(mean(r^2)) with sampleSec scaling", () => {
    const rv = new RealizedVol(3600, 2, 1);
    // Sample every 2 s; returns of exactly +/-0.01 alternating.
    let p = 100;
    rv.push(0, p);
    for (let i = 1; i <= 10; i++) {
      p *= Math.exp(i % 2 === 0 ? 0.01 : -0.01);
      rv.push(i * 2000, p);
    }
    // 10 closed returns of magnitude 0.01 each.
    expect(rv.count()).toBe(9);
    expect(rv.sigma()).toBeCloseTo(0.01 / Math.sqrt(2), 9);
  });

  it("lookback trimming drops old returns", () => {
    const rv = new RealizedVol(300, 1, 1);
    let p = 100000;
    let t = 0;
    rv.push(t, p);
    // Phase A: large +/-5% alternating moves for 500 s.
    for (let i = 0; i < 500; i++) {
      t += 1000;
      p *= Math.exp(i % 2 === 0 ? 0.05 : -0.05);
      rv.push(t, p);
    }
    const untrimmedBig = rv.sigma();
    expect(untrimmedBig).toBeCloseTo(0.05, 6);
    // Phase B: small +/-0.1% alternating moves for 400 s. Window is 300 s, so only B remains.
    for (let i = 0; i < 400; i++) {
      t += 1000;
      p *= Math.exp(i % 2 === 0 ? 0.001 : -0.001);
      rv.push(t, p);
    }
    expect(rv.count()).toBeLessThanOrEqual(301);
    expect(rv.count()).toBeGreaterThanOrEqual(300);
    expect(rv.sigma()).toBeCloseTo(0.001, 9);
  });

  it("setLookback shrinks the window", () => {
    const rv = new RealizedVol(3600, 1, 1);
    const rng = mulberry32(5);
    let p = 100000;
    for (let i = 0; i <= 600; i++) {
      p *= Math.exp(gauss(rng) * 1e-3);
      rv.push(i * 1000, p);
    }
    const before = rv.count();
    rv.setLookback(100);
    expect(rv.count()).toBeLessThan(before);
    expect(rv.count()).toBeLessThanOrEqual(101);
  });

  it("weights returns by the time they cover (mixed 60s seeded + 1s live samples)", () => {
    const rv = new RealizedVol(3600, 1, 1);
    // 10 seeded 60s returns of 0.006 (variance 3.6e-5 over 60 s = 6e-7/s), then 60 live 1s returns of 0.001 (1e-6/s).
    let t = 0;
    let p = 100000;
    rv.push(t + 59_999, p);
    for (let i = 0; i < 10; i++) {
      t += 60_000;
      p *= Math.exp(i % 2 === 0 ? 0.006 : -0.006);
      rv.push(t + 59_999, p);
    }
    const t1 = t + 60_000; // live data starts right after the last seeded close
    for (let i = 1; i <= 61; i++) {
      p *= Math.exp(i % 2 === 0 ? 0.001 : -0.001);
      rv.push(t1 + i * 1000, p);
    }
    // closed: 10 seeded (600 s) + 1 bridging return + 59 live; time-weighted, not count-weighted
    const span = rv.spanSec();
    expect(span).toBeGreaterThanOrEqual(600 + 59);
    const est = rv.sigma()!;
    const seededVar = 10 * 0.006 ** 2;
    expect(est ** 2).toBeGreaterThan(seededVar / span); // the seeded hour still counts
    expect(est).toBeLessThan(0.001); // a count-weighted mean would be ~0.0012
    expect(est).toBeGreaterThan(Math.sqrt(6e-7));
  });

  it("spanSec reports the covered history and shrinks with the lookback", () => {
    const rv = new RealizedVol(3600, 1, 1);
    expect(rv.spanSec()).toBe(0);
    for (let i = 0; i <= 120; i++) rv.push(i * 1000, 100 + (i % 2));
    expect(rv.spanSec()).toBe(119);
    rv.push(200_000, 100); // closes bucket 120
    rv.push(201_000, 101); // closes bucket 200: the 80 s gap return covers 80 s
    expect(rv.spanSec()).toBe(120 + 80);
    rv.setLookback(50);
    expect(rv.spanSec()).toBeLessThanOrEqual(80);
  });

  it("ignores invalid and out-of-order points", () => {
    const rv = new RealizedVol(3600, 1, 1);
    rv.push(NaN, 100);
    rv.push(0, -1);
    rv.push(0, 0);
    rv.push(0, Infinity);
    rv.push(5000, 100);
    rv.push(4000, 999); // out of order, ignored
    rv.push(6000, 101);
    rv.push(7000, 102);
    expect(rv.count()).toBe(1);
  });
});
