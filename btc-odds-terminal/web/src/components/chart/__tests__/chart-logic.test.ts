// Unit tests for the chart's pure logic (no canvas / DOM). Run from web/:
//   NODE_OPTIONS=--conditions=import ../backend/node_modules/.bin/tsx --test src/components/chart/__tests__/*.test.ts
// (the "import" condition lets the CJS-loaded sources resolve the ESM-only lightweight-charts package)

import { test } from "node:test";
import assert from "node:assert/strict";
import type { MarketWindow } from "@shared/protocol";
import type { HoverInfo } from "@/lib/store";
import { BucketAggregator, bucketTimes, type CandleAgg } from "../aggregate";
import { sameHover } from "../hover";
import { contrastText, dodge } from "../primitives/LastValue";
import { fitLabel, labelVariants } from "../primitives/WindowBands";

interface Pt {
  ts: number;
  v: number;
}
const agg = (slack: number) =>
  new BucketAggregator<Pt, CandleAgg>(
    (p) => p.ts,
    (acc, p, time) => {
      if (!acc) return { time, open: p.v, high: p.v, low: p.v, close: p.v };
      acc.high = Math.max(acc.high, p.v);
      acc.low = Math.min(acc.low, p.v);
      acc.close = p.v;
      return acc;
    },
    slack,
  );
const pts = (fromSec: number, n: number) => Array.from({ length: n }, (_, i) => ({ ts: (fromSec + i) * 1000, v: i }));

test("aggregator: appends report a tail, front trims within the slack are kept", () => {
  const a = agg(5);
  const src = pts(0, 10);
  assert.equal(a.sync(src, 1).kind, "full");
  src.push({ ts: 10_000, v: 10 });
  assert.deepEqual(a.sync(src, 1), { kind: "tail", from: 9 });
  src.splice(0, 3); // ingest trimFront
  src.push({ ts: 11_000, v: 11 });
  assert.equal(a.sync(src, 1).kind, "tail");
  assert.equal(a.out.length, 12);
  assert.equal(a.out[0].time, 0);
});

test("aggregator: drops buckets older than the trimmed source once past the slack", () => {
  const a = agg(5);
  const src = pts(0, 20);
  a.sync(src, 1);
  src.splice(0, 8);
  src.push({ ts: 20_000, v: 20 });
  assert.equal(a.sync(src, 1).kind, "full");
  assert.equal(a.out.length, 13);
  assert.equal(a.out[0].time, 8);
  // and is back to cheap tails afterwards
  src.push({ ts: 21_000, v: 21 });
  assert.equal(a.sync(src, 1).kind, "tail");
});

test("bucketTimes covers every bucket inclusively, aligned to the timeframe", () => {
  assert.deepEqual(bucketTimes(62, 120, 15), [60, 75, 90, 105, 120]);
  assert.deepEqual(bucketTimes(0, 3, 1), [0, 1, 2, 3]);
  assert.deepEqual(bucketTimes(10, 5, 1), []);
});

const hover = (p: Partial<HoverInfo> = {}): HoverInfo => ({
  time: 1_000,
  o: 1,
  h: 2,
  l: 0.5,
  c: 1.5,
  upMid: 44.5,
  upBid: 44,
  upAsk: 45,
  fair: 46,
  edge: 1.5,
  ptb: 1.2,
  tLeftSec: 120.4,
  ...p,
});

test("sameHover compares values, so a live candle under a parked cursor republishes", () => {
  assert.ok(sameHover(hover(), hover()));
  assert.ok(!sameHover(hover(), hover({ c: 1.4 }))); // same time, close moved
  assert.ok(!sameHover(hover(), hover({ upMid: 44 })));
  assert.ok(!sameHover(hover(), hover({ h: 2.1 })));
  assert.ok(!sameHover(hover(), null));
  assert.ok(sameHover(null, null));
  // the countdown only matters per whole second
  assert.ok(sameHover(hover({ tLeftSec: 120.4 }), hover({ tLeftSec: 120.9 })));
  assert.ok(!sameHover(hover({ tLeftSec: 120.4 }), hover({ tLeftSec: 119.9 })));
  assert.ok(!sameHover(hover({ tLeftSec: null }), hover({ tLeftSec: 0 })));
});

test("dodge keeps a tag clear of a fixed label but leaves distant tags alone", () => {
  assert.equal(dodge(100, null), 100);
  assert.equal(dodge(100, 150), 100);
  assert.equal(dodge(100, 110, 19), 91);
  assert.equal(dodge(115, 110, 19), 129);
});

test("contrastText matches lightweight-charts' label text rule", () => {
  assert.equal(contrastText("#34D399"), "#0A0D12"); // up green -> dark text
  assert.equal(contrastText("#F87171"), "#FFFFFF"); // down red -> white text
  assert.equal(contrastText("#5B9CFF"), "#FFFFFF");
});

const win = (p: Partial<MarketWindow>): MarketWindow =>
  ({
    id: "w",
    startTs: Date.UTC(2026, 9, 20, 14, 15),
    endTs: Date.UTC(2026, 9, 20, 14, 30),
    status: "settled",
    result: "up",
    priceToBeat: 67000,
    ...p,
  }) as MarketWindow;
const text = (runs: { text: string }[] | null) => (runs ? runs.map((r) => r.text).join("") : null);

test("band labels fall back from full label to status, range, then compact status", () => {
  const v = labelVariants(win({}), false);
  assert.deepEqual(v.map(text), ["10:15–10:30 · SETTLED UP", "SETTLED UP", "10:15–10:30", "UP"]);
  const w = (runs: { text: string }[]) => text(runs)!.length * 6;
  assert.equal(text(fitLabel(v, 1000, w)), "10:15–10:30 · SETTLED UP");
  assert.equal(text(fitLabel(v, 100, w)), "SETTLED UP");
  assert.equal(text(fitLabel(v, 30, w)), "UP");
  assert.equal(fitLabel(v, 5, w), null);
  assert.deepEqual(labelVariants(win({}), true).map(text), ["10:15–10:30 · LIVE", "LIVE", "10:15–10:30"]);
  assert.deepEqual(labelVariants(win({}), true, true).map(text), ["10:15–10:30 · REPLAY", "REPLAY", "10:15–10:30"]);
  assert.deepEqual(labelVariants(win({ status: "upcoming", result: null }), false).map(text), ["10:15–10:30"]);
});
