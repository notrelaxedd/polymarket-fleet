// Pair relationship card geometry: scales, ticks, dot / line paths, time-left buckets, lag peak + label,
// sensitivity window / domain, label separation. Run: tsx --test src/components/tabs/__tests__/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import type { LagBar, ScatterPoint, SensitivityPoint } from "@shared/protocol";
import {
  TLEFT_BUCKETS,
  dotsPath,
  lagDomain,
  lagPeakLabel,
  lastValue,
  linePath,
  linear,
  niceTicks,
  quantile,
  resolveLagPeak,
  sampleEvenly,
  scatterLayers,
  sensitivityDomain,
  sensitivityWindow,
  separateLabels,
  tLeftBucket,
} from "../pair/geom";

test("linear maps domain to range (and a flat domain to the middle)", () => {
  const x = linear(-2, 2, 0, 100);
  assert.equal(x(-2), 0);
  assert.equal(x(0), 50);
  assert.equal(x(2), 100);
  const y = linear(0, 100, 90, 10); // inverted y
  assert.equal(y(100), 10);
  assert.equal(linear(5, 5, 0, 10)(123), 5);
});

test("niceTicks picks 1/2/5 steps inside the domain", () => {
  assert.deepEqual(niceTicks(0, 1, 4), [0, 0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(niceTicks(-0.2, 0.8, 2), [0, 0.5]);
  assert.deepEqual(niceTicks(0, 100, 2), [0, 50, 100]);
  assert.deepEqual(niceTicks(3, 3), [3]);
  assert.deepEqual(niceTicks(NaN, 1), []);
});

test("time-left buckets go dark slate (early) -> light blue (late)", () => {
  assert.equal(TLEFT_BUCKETS.length, 4);
  assert.equal(tLeftBucket(899), 0);
  assert.equal(tLeftBucket(600), 0);
  assert.equal(tLeftBucket(599), 1);
  assert.equal(tLeftBucket(300), 1);
  assert.equal(tLeftBucket(200), 2);
  assert.equal(tLeftBucket(119), 3);
  assert.equal(tLeftBucket(0), 3);
  assert.equal(tLeftBucket(-5), 3);
});

test("dotsPath emits one zero-length segment per finite point", () => {
  assert.equal(dotsPath([{ x: 1, y: 2 }, { x: NaN, y: 1 }, { x: 3.25, y: 4 }]), "M1.0 2.0h0M3.3 4.0h0");
  assert.equal(dotsPath([]), "");
});

test("linePath breaks at null values", () => {
  assert.equal(linePath([[0, 1], [1, 2], [2, null], [3, 4], [4, 5]]), "M0.0 1.0L1.0 2.0M3.0 4.0L4.0 5.0");
  assert.equal(linePath([[0, null]]), "");
});

test("sampleEvenly keeps order, size cap and the newest item", () => {
  const a = Array.from({ length: 100 }, (_, i) => i);
  const s = sampleEvenly(a, 10);
  assert.equal(s.length, 10);
  assert.equal(s[0], 0);
  assert.equal(s[9], 99);
  assert.ok(s.every((v, i) => i === 0 || v > s[i - 1]));
  assert.deepEqual(sampleEvenly([1, 2], 5), [1, 2]);
  assert.deepEqual(sampleEvenly([1, 2], 0), []);
});

test("quantile interpolates and ignores non-finite values", () => {
  assert.equal(quantile([1, 2, 3, 4, 5], 0.5), 3);
  assert.equal(quantile([0, 10], 0.25), 2.5);
  assert.equal(quantile([NaN, Infinity], 0.5), null);
});

test("scatterLayers buckets by time left, drops off-scale points and clamps y", () => {
  const p = (z: number, upMid: number, tLeftSec: number): ScatterPoint => ({ ts: 0, windowId: "w", z, upMid, tLeftSec });
  const pts = [p(0, 50, 800), p(1, 70, 400), p(-1, 30, 200), p(1.5, 120, 10), p(3, 99, 10), p(-2.5, 1, 700)];
  const { layers, shown, offScale } = scatterLayers(pts, (z) => z * 10, (c) => c);
  assert.equal(shown, 4);
  assert.equal(offScale, 2);
  assert.deepEqual(layers.map((l) => l.pts.length), [1, 1, 1, 1]);
  assert.deepEqual(layers[3].pts[0], { x: 15, y: 100 }); // upMid clamped to 100
  assert.equal(layers[0].color, TLEFT_BUCKETS[0].color);
});

test("lag peak: payload peak wins, else highest rho; label format", () => {
  const bars: LagBar[] = [
    { lagSec: 0, rho: 0.2, n: 100 },
    { lagSec: 2, rho: 0.45, n: 100 },
    { lagSec: 4, rho: 0.61, n: 100 },
    { lagSec: 6, rho: null, n: 0 },
  ];
  assert.deepEqual(resolveLagPeak(bars, null), { lagSec: 4, rho: 0.61 });
  assert.deepEqual(resolveLagPeak(bars, { lagSec: 2, rho: 0.45 }), { lagSec: 2, rho: 0.45 });
  assert.deepEqual(resolveLagPeak(bars, { lagSec: 40, rho: 0.9 }), { lagSec: 4, rho: 0.61 }); // unknown lag ignored
  assert.equal(resolveLagPeak([{ lagSec: 0, rho: null, n: 0 }], null), null);
  assert.equal(lagPeakLabel({ lagSec: 4, rho: 0.6123 }), "4s · ρ 0.61");
  assert.equal(lagPeakLabel({ lagSec: 0, rho: -0.2 }), "0s · ρ −0.20");
  assert.equal(lagPeakLabel(null), null);
});

test("lagDomain contains zero and leaves headroom above the peak", () => {
  const [lo, hi] = lagDomain([{ lagSec: 0, rho: 0.6, n: 1 }]);
  assert.equal(lo, 0);
  assert.ok(hi > 0.6 && hi <= 1);
  const [lo2] = lagDomain([{ lagSec: 0, rho: -0.3, n: 1 }]);
  assert.ok(lo2 < -0.3);
  assert.deepEqual(lagDomain([]), [0, 0.2 * 1.3]);
});

test("sensitivity: window from ts + tLeft, robust domain, last values", () => {
  const end = 1_700_000_900_000;
  const pts: SensitivityPoint[] = Array.from({ length: 300 }, (_, i) => {
    const ts = end - 900_000 + i * 1000;
    return { ts, tLeftSec: (end - ts) / 1000, market: i === 150 ? 500 : 1 + i / 300, fair: i === 299 ? null : 1.2 };
  });
  assert.deepEqual(sensitivityWindow(pts), { startTs: end - 900_000, endTs: end });
  assert.equal(sensitivityWindow([]), null);
  const [lo, hi] = sensitivityDomain(pts);
  assert.equal(lo, 0);
  assert.ok(hi < 10, `outlier must not blow up the domain (hi=${hi})`);
  assert.deepEqual(sensitivityDomain([]), [0, 1]);
  assert.equal(lastValue(pts, "fair")?.v, 1.2);
  assert.equal(lastValue(pts, "fair")?.ts, pts[298].ts);
  assert.equal(lastValue([], "market"), null);
});

test("separateLabels keeps two labels at least `gap` apart inside the plot", () => {
  assert.deepEqual(separateLabels(20, 60, 10, 0, 100), [20, 60]);
  const [a, b] = separateLabels(50, 52, 10, 0, 100);
  assert.ok(a! < b!);
  assert.ok(Math.abs(a! - b!) >= 10 - 1e-9);
  const [c, d] = separateLabels(1, 2, 10, 0, 100);
  assert.ok(Math.min(c!, d!) >= 0);
  assert.ok(Math.abs(c! - d!) >= 10 - 1e-9);
  assert.deepEqual(separateLabels(null, 200, 10, 0, 100), [null, 100]);
});
