// Incremental time-bucket aggregation of the store's live series.
//
// The ingest layer keeps `series.*` as ascending arrays that are mutated in place (push, or replace the
// last element for the same second) and occasionally trimmed at the FRONT (splice). A history load swaps
// in brand-new array objects. So this aggregator:
//  - rebuilds everything when the source array identity or the timeframe changes ("full"),
//  - otherwise re-folds only the LAST output bucket plus anything after it ("tail"), locating the start
//    of that bucket in the source by binary search (never by stored index — front trims shift indices).
// Per frame the work is O(log n + items in the last bucket), independent of how much history is loaded.
// Front trims of the source are mirrored lazily: once more than TRIM_SLACK output buckets precede the
// source's first bucket, the next sync rebuilds ("full"), so `out` stays bounded by the source window.

import type { Bar, BtcTick, ExchangeId, FairSnap, OddsSnap } from "@shared/protocol";

export interface Bucketed {
  /** bucket open time, epoch SECONDS (lightweight-charts UTCTimestamp) */
  time: number;
}

export type SyncResult =
  | { kind: "none" }
  | { kind: "full" }
  /** out[from..] changed; out[from] has the same time as the chart's current last item or later */
  | { kind: "tail"; from: number };

const NONE: SyncResult = { kind: "none" };
const FULL: SyncResult = { kind: "full" };

/** Output buckets allowed to outlive the (front-trimmed) source before a rebuild drops them. */
export const TRIM_SLACK = 2000;

export class BucketAggregator<S, O extends Bucketed> {
  out: O[] = [];
  private src: readonly S[] | null = null;
  private tfMs = 0;
  private lastLen = -1;
  private lastRef: S | undefined = undefined;

  constructor(
    private readonly tsOf: (s: S) => number,
    /** fold one source item into the bucket accumulator (acc is null for the first item of a bucket) */
    private readonly fold: (acc: O | null, s: S, bucketSec: number) => O,
    private readonly trimSlack = TRIM_SLACK,
  ) {}

  get timeframeMs() {
    return this.tfMs;
  }

  reset() {
    this.src = null;
    this.out = [];
    this.lastLen = -1;
    this.lastRef = undefined;
  }

  sync(src: readonly S[], tfSec: number): SyncResult {
    const tfMs = tfSec * 1000;
    if (src !== this.src || tfMs !== this.tfMs) {
      this.src = src;
      this.tfMs = tfMs;
      this.out = [];
      this.foldFrom(0);
      this.mark();
      return FULL;
    }
    const n = src.length;
    if (n === this.lastLen && src[n - 1] === this.lastRef) return NONE;
    if (n === 0) {
      const had = this.out.length > 0;
      this.out = [];
      this.mark();
      return had ? FULL : NONE;
    }
    if (this.out.length === 0 || this.surplus() > this.trimSlack) {
      // first data, or the source was trimmed far enough at the front: rebuild from what is left
      this.out = [];
      this.foldFrom(0);
      this.mark();
      return FULL;
    }
    const lastBucketMs = this.out[this.out.length - 1].time * 1000;
    const i = this.lowerBound(lastBucketMs);
    if (i >= n) {
      // the last bucket's items were trimmed away (should not happen at the tail) — nothing to redo
      this.mark();
      return NONE;
    }
    const from = this.out.length - 1;
    this.out.length = from;
    this.foldFrom(i);
    this.mark();
    return { kind: "tail", from: Math.min(from, this.out.length - 1) };
  }

  /** number of output buckets older than the source's first bucket (left behind by front trims) */
  private surplus(): number {
    const src = this.src!;
    const firstBucketSec = (Math.floor(this.tsOf(src[0]) / this.tfMs) * this.tfMs) / 1000;
    return lowerBoundTime(this.out, firstBucketSec);
  }

  private mark() {
    const src = this.src!;
    this.lastLen = src.length;
    this.lastRef = src[src.length - 1];
  }

  private lowerBound(ms: number): number {
    const src = this.src!;
    let lo = 0;
    let hi = src.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.tsOf(src[mid]) < ms) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  private foldFrom(i: number) {
    const src = this.src!;
    const tf = this.tfMs;
    const out = this.out;
    let acc: O | null = null;
    let cur = -Infinity;
    for (let k = i; k < src.length; k++) {
      const s = src[k];
      const b = Math.floor(this.tsOf(s) / tf) * tf;
      if (b !== cur) {
        if (acc) out.push(acc);
        acc = null;
        cur = b;
      }
      acc = this.fold(acc, s, b / 1000);
    }
    if (acc) out.push(acc);
  }
}

/** Index of the item with exactly `timeSec`, else the last item before it, else -1. */
export function indexAtOrBefore<T extends Bucketed>(arr: readonly T[], timeSec: number): number {
  let lo = 0;
  let hi = arr.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid].time <= timeSec) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

export function findAt<T extends Bucketed>(arr: readonly T[], timeSec: number): T | null {
  const i = indexAtOrBefore(arr, timeSec);
  return i >= 0 && arr[i].time === timeSec ? arr[i] : null;
}

/** Every bucket open time (seconds) from the bucket containing `fromSec` through `toSec`, inclusive. */
export function bucketTimes(fromSec: number, toSec: number, tfSec: number): number[] {
  const out: number[] = [];
  for (let t = Math.floor(fromSec / tfSec) * tfSec; t <= toSec; t += tfSec) out.push(t);
  return out;
}

/** First index with time >= timeSec. */
export function lowerBoundTime<T extends Bucketed>(arr: readonly T[], timeSec: number): number {
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid].time < timeSec) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// ---- concrete aggregates ---------------------------------------------------------------------------

export interface CandleAgg extends Bucketed {
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface OddsAgg extends Bucketed {
  upMid: number | null;
  upBid: number | null;
  upAsk: number | null;
  downMid: number | null;
  windowId: string;
}

export interface FairAgg extends Bucketed {
  fair: number;
  edge: number | null;
  tLeftSec: number;
  windowId: string;
}

export type TickAgg = Bucketed & Partial<Record<ExchangeId, number>>;

export const makeBarAgg = () =>
  new BucketAggregator<Bar, CandleAgg>(
    (b) => b.t,
    (acc, b, time) => {
      if (!acc) return { time, open: b.o, high: b.h, low: b.l, close: b.c };
      if (b.h > acc.high) acc.high = b.h;
      if (b.l < acc.low) acc.low = b.l;
      acc.close = b.c;
      return acc;
    },
  );

const mid = (b: number | null, a: number | null) => (b != null && a != null ? (b + a) / 2 : (b ?? a));

export const makeOddsAgg = () =>
  new BucketAggregator<OddsSnap, OddsAgg>(
    (o) => o.ts,
    (acc, o, time) => {
      const downMid = mid(o.downBid, o.downAsk);
      if (!acc || acc.windowId !== o.windowId)
        return { time, upMid: o.upMid, upBid: o.upBid, upAsk: o.upAsk, downMid, windowId: o.windowId };
      // last value in the bucket wins; keep the previous one where the latest snap has a hole
      acc.upMid = o.upMid ?? acc.upMid;
      acc.upBid = o.upBid ?? acc.upBid;
      acc.upAsk = o.upAsk ?? acc.upAsk;
      acc.downMid = downMid ?? acc.downMid;
      return acc;
    },
  );

export const makeFairAgg = () =>
  new BucketAggregator<FairSnap, FairAgg>(
    (f) => f.ts,
    (acc, f, time) => {
      if (!acc || acc.windowId !== f.windowId)
        return { time, fair: f.fair, edge: f.edge, tLeftSec: f.tLeftSec, windowId: f.windowId };
      acc.fair = f.fair;
      acc.edge = f.edge ?? acc.edge;
      acc.tLeftSec = f.tLeftSec;
      return acc;
    },
  );

export const makeTickAgg = () =>
  new BucketAggregator<BtcTick, TickAgg>(
    (t) => t.ts,
    (acc, t, time) => {
      const a: TickAgg = acc ?? { time };
      const s = t.sources;
      if (s.coinbase != null) a.coinbase = s.coinbase;
      if (s.kraken != null) a.kraken = s.kraken;
      if (s.bitstamp != null) a.bitstamp = s.bitstamp;
      return a;
    },
  );
