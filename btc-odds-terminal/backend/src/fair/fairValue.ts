/**
 * Fair-value model for "BTC Up" binary markets.
 *
 * Under a driftless lognormal price model with per-√second volatility sigma,
 * P(S_T > K) = Φ( ln(S/K) / (sigma·√t) ).
 */

/**
 * Standard normal CDF Φ(x).
 *
 * Hart's double-precision rational/continued-fraction approximation
 * (as published by G. West, 2005, "Better approximations to cumulative normal
 * functions"). Absolute error is far below 1e-7 over the whole real line.
 */
export function normCdf(x: number): number {
  if (Number.isNaN(x)) return NaN;
  const ax = Math.abs(x);
  let c: number;
  if (ax > 37) {
    c = 0;
  } else {
    const e = Math.exp(-0.5 * ax * ax);
    if (ax < 7.07106781186547) {
      let b = 3.52624965998911e-2 * ax + 0.700383064443688;
      b = b * ax + 6.37396220353165;
      b = b * ax + 33.912866078383;
      b = b * ax + 112.079291497871;
      b = b * ax + 221.213596169931;
      b = b * ax + 220.206867912376;
      c = e * b;
      b = 8.83883476483184e-2 * ax + 1.75566716318264;
      b = b * ax + 16.064177579207;
      b = b * ax + 86.7807322029461;
      b = b * ax + 296.564248779674;
      b = b * ax + 637.333633378831;
      b = b * ax + 793.826512519948;
      b = b * ax + 440.413735824752;
      c = c / b;
    } else {
      let b = ax + 0.65;
      b = ax + 4 / b;
      b = ax + 3 / b;
      b = ax + 2 / b;
      b = ax + 1 / b;
      c = e / b / 2.506628274631;
    }
  }
  return x > 0 ? 1 - c : c;
}

function clamp01(p: number): number {
  return p < 0 ? 0 : p > 1 ? 1 : p;
}

/**
 * P(BTC finishes above K) given spot S, strike K, seconds remaining tSec and
 * realized vol sigma (fractional log-return per √second).
 *
 * - S or K not finite-positive, or sigma non-finite, or tSec NaN -> NaN
 * - tSec <= 0 or sigma <= 0 -> step: 1 if S > K, 0 if S < K, 0.5 if equal
 */
export function fairUp(S: number, K: number, tSec: number, sigma: number): number {
  if (!(Number.isFinite(S) && S > 0 && Number.isFinite(K) && K > 0)) return NaN;
  if (Number.isNaN(tSec) || !Number.isFinite(sigma)) return NaN;
  if (tSec <= 0 || sigma <= 0) return S > K ? 1 : S < K ? 0 : 0.5;
  if (S === K) return 0.5;
  const d = Math.log(S / K) / (sigma * Math.sqrt(tSec));
  return clamp01(normCdf(d));
}

/** Fair P(Up) in cents (0-100). */
export function fairCents(S: number, K: number, tSec: number, sigma: number): number {
  return 100 * fairUp(S, K, tSec, sigma);
}

/**
 * Edge in cents: fair value minus the market's Up midpoint (cents).
 * Returns null when the midpoint is missing or either input is non-finite.
 */
export function edgeCents(fairC: number, upMidC: number | null): number | null {
  if (upMidC === null || !Number.isFinite(upMidC) || !Number.isFinite(fairC)) return null;
  return fairC - upMidC;
}

/**
 * Rolling realized volatility per √second from a price stream.
 *
 * Prices are resampled to `sampleSec` buckets (the last price seen in each
 * bucket wins). Each bucket close yields one log return against the previous
 * bucket close. A return spanning n buckets (a gap) is normalised so it
 * carries the variance of a single sample: its squared value is divided by n.
 * sigma = sqrt(mean(contribution) / sampleSec) over returns whose closing bucket
 * lies within the last `lookbackSec`.
 *
 * Because a bucket's return is only known once the bucket closes, the most
 * recent bucket is included after the next bucket starts (one-sample delay).
 * Pushes are O(1) amortised: a ring buffer plus a running sum.
 */
export class RealizedVol {
  private lookbackMs: number;
  private readonly sampleSec: number;
  private readonly sampleMs: number;
  private readonly minSamples: number;

  /** Bucket currently accumulating (its last price is not final yet). */
  private curBucket: number | null = null;
  private curPrice = 0;
  /** Most recently closed bucket. */
  private prevBucket: number | null = null;
  private prevPrice = 0;

  // Ring buffer of return contributions, keyed by closing-bucket time (ms).
  private cap = 256;
  private times = new Float64Array(this.cap);
  private vals = new Float64Array(this.cap);
  private head = 0;
  private size = 0;
  private sumV = 0;

  constructor(lookbackSec: number, sampleSec = 1, minSamples = 30) {
    if (!(Number.isFinite(lookbackSec) && lookbackSec > 0)) {
      throw new RangeError(`lookbackSec must be a positive finite number, got ${lookbackSec}`);
    }
    if (!(Number.isFinite(sampleSec) && sampleSec > 0)) {
      throw new RangeError(`sampleSec must be a positive finite number, got ${sampleSec}`);
    }
    if (!(Number.isInteger(minSamples) && minSamples >= 1)) {
      throw new RangeError(`minSamples must be an integer >= 1, got ${minSamples}`);
    }
    this.lookbackMs = lookbackSec * 1000;
    this.sampleSec = sampleSec;
    this.sampleMs = sampleSec * 1000;
    this.minSamples = minSamples;
  }

  /** Feed one trade/quote price. ts in milliseconds. Invalid or out-of-order points are ignored. */
  push(ts: number, price: number): void {
    if (!Number.isFinite(ts) || !Number.isFinite(price) || price <= 0) return;
    const bucket = Math.floor(ts / this.sampleMs);
    if (this.curBucket === null) {
      this.curBucket = bucket;
      this.curPrice = price;
      return;
    }
    if (bucket === this.curBucket) {
      this.curPrice = price;
      return;
    }
    if (bucket < this.curBucket) return;
    this.closeBucket(this.curBucket, this.curPrice);
    this.curBucket = bucket;
    this.curPrice = price;
  }

  /** Realized vol per √second over the lookback window, or null if fewer than minSamples returns. */
  sigma(): number | null {
    if (this.size < this.minSamples) return null;
    const meanV = Math.max(0, this.sumV) / this.size;
    return Math.sqrt(meanV / this.sampleSec);
  }

  setLookback(lookbackSec: number): void {
    if (!(Number.isFinite(lookbackSec) && lookbackSec > 0)) {
      throw new RangeError(`lookbackSec must be a positive finite number, got ${lookbackSec}`);
    }
    this.lookbackMs = lookbackSec * 1000;
    if (this.prevBucket !== null) this.trim(this.prevBucket * this.sampleMs);
  }

  /** Number of return contributions currently inside the lookback window. */
  count(): number {
    return this.size;
  }

  private closeBucket(bucket: number, price: number): void {
    if (this.prevBucket !== null) {
      const n = bucket - this.prevBucket;
      const r = Math.log(price / this.prevPrice);
      this.append(bucket * this.sampleMs, (r * r) / n);
    }
    this.prevBucket = bucket;
    this.prevPrice = price;
    this.trim(bucket * this.sampleMs);
  }

  private trim(latestMs: number): void {
    const cutoff = latestMs - this.lookbackMs;
    while (this.size > 0 && this.times[this.head]! < cutoff) {
      this.sumV -= this.vals[this.head]!;
      this.head = (this.head + 1) % this.cap;
      this.size--;
    }
    if (this.size === 0) {
      this.sumV = 0;
      this.head = 0;
    }
  }

  private append(timeMs: number, v: number): void {
    if (this.size === this.cap) this.grow();
    const idx = (this.head + this.size) % this.cap;
    this.times[idx] = timeMs;
    this.vals[idx] = v;
    this.size++;
    this.sumV += v;
  }

  private grow(): void {
    const newCap = this.cap * 2;
    const times = new Float64Array(newCap);
    const vals = new Float64Array(newCap);
    for (let i = 0; i < this.size; i++) {
      const src = (this.head + i) % this.cap;
      times[i] = this.times[src]!;
      vals[i] = this.vals[src]!;
    }
    this.times = times;
    this.vals = vals;
    this.cap = newCap;
    this.head = 0;
  }
}
