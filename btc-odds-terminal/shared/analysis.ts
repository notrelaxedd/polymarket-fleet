// Pure pair-relationship math: scatter (BTC vs strike -> Up mid), lag (BTC returns vs later odds changes),
// sensitivity (cents of Up per $10 BTC, market vs fair) and the one-line takeaways. OWNER: analysis.
//
// Shared by backend/src/analysis (which adds the DB reads, a per-window cache of finished windows and a per-scope
// TTL cache on top) and the web mock, so both show the same numbers. Imports are extensionless (tsx + Next.js).
//
// Everything is computed on a 1s grid per window ("bucket" k = second startSec + k):
//   btc[k]   close of the 1s BRTI-proxy bar of that second (forward-filled up to BTC_FILL_SEC)
//   mid[k]   Up mid of the last odds row recorded in that second with BOTH sides quoted and a sane spread
//            (validUpMid; forward-filled up to ODDS_FILL_SEC, midAge[k] = seconds since that row)
//   sigma[k] realized vol of the last fair row (forward-filled up to FAIR_FILL_SEC)
// Series never cross window boundaries (each window is its own market).
// btc[k] and mid[k] are end-of-second values, so time left at bucket k is endTs - (second k + 1).
// Lag / sensitivity pairs and scatter samples only use mids at most ODDS_MAX_AGE_SEC old: a stalled feed's
// forward-filled mid would otherwise enter as zero odds changes against real BTC moves (biasing both low).

import type {
  AnalysisPayload,
  AnalysisScope,
  Bar,
  FairSnap,
  LagBar,
  MarketWindow,
  OddsSnap,
  ScatterPoint,
  SensitivityPoint,
} from "./protocol";

export interface AnalysisInput {
  scope: AnalysisScope;
  now: number;
  /** windows in scope (with priceToBeat), any order */
  windows: MarketWindow[];
  /** 1s bars covering those windows, ascending (bars with d !== 1 are ignored) */
  bars: Bar[];
  odds: OddsSnap[];
  fair: FairSnap[];
  current: MarketWindow | null;
}

/** Lags evaluated by the lag card: 0..30s in 2s steps. */
export const LAG_STEPS_SEC: number[] = Array.from({ length: 16 }, (_, i) => i * 2);

// ---------------------------------------------------------------- tuning

/** |z| is clipped to this for the scatter (x axis -2..+2). */
export const Z_CLIP = 2;
/** One scatter sample every this many seconds per window. */
export const SCATTER_STRIDE_SEC = 2;
/** Max scatter points returned per scope (uniform downsampling across the scope beyond that). */
export const MAX_SCATTER_POINTS: Record<AnalysisScope, number> = { window: 600, last20: 2500, all: 2500 };
/** Samples closer than this to the window end (end-of-second time left) are skipped (z explodes as t -> 0). */
export const MIN_TLEFT_SEC = 1;
/** An Up mid counts only with both sides quoted and the spread at most this (cents); one-sided books give none. */
export const MAX_MID_SPREAD_CENTS = 20;
/** Max age (s) of the odds observation behind a mid used in a lag / sensitivity pair or a scatter sample. */
export const ODDS_MAX_AGE_SEC = 2;
export const BTC_FILL_SEC = 5;
export const ODDS_FILL_SEC = 15;
export const FAIR_FILL_SEC = 5;
/** Min pairs for a lag correlation. */
export const MIN_LAG_N = 30;
/** Sensitivity regression: rolling window, change horizon, min pairs, point spacing. */
export const SENS_WINDOW_SEC = 60;
export const SENS_HORIZON_SEC = 5;
export const SENS_MIN_N = 30;
export const SENS_POINT_STEP_SEC = 5;
/** BTC changes in the regression window must have at least this variance ($^2) for a market slope. */
export const SENS_MIN_VAR_X = 4;
/**
 * No sensitivity points closer than this to the window end: dFair/dS ~ 1/sqrt(t) blows up (and the market slope
 * gets noisy), which would dominate the card's y-axis and the takeaway in the last seconds.
 */
export const SENS_MIN_TLEFT_SEC = 30;
/** The measured lag peak shifts the sensitivity regression when its rho is at least this; capped at MAX. */
export const SENS_SHIFT_MIN_RHO = 0.15;
export const SENS_MAX_SHIFT_SEC = 10;
/** The "now" point is only shown from data at most this old. */
export const CURRENT_MAX_AGE_MS = 5_000;
/** Min samples on a side of the strike for the scope-wide scatter takeaway. */
const MIN_SIDE_SAMPLES = 20;
/** |z| below this counts as "at the strike" for the scope-wide takeaway. */
const AT_STRIKE_Z = 0.25;
const FAIR_CURVE_STEP = 0.1;
const LAG_FIELDS = 6; // n, sx, sy, sxx, syy, sxy

export const NOT_ENOUGH = "Not enough data yet.";
/** Sensitivity takeaway for scope "window" when nothing is live (scatter / lag then show the last window). */
export const NO_LIVE_WINDOW = "No live window right now; showing the last window.";

// ---------------------------------------------------------------- normal distribution

/**
 * Standard normal CDF (Hart / West 2005 double-precision approximation; same algorithm as
 * backend/src/fair/fairValue.ts normCdf, duplicated so this file stays backend-free).
 */
export function phi(x: number): number {
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

/** Standard normal density. */
export function phiPdf(x: number): number {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

/** z = ln(S/K) / (sigma * sqrt(tLeft)); null when any input is unusable. */
export function zScore(S: number, K: number, sigma: number, tLeftSec: number): number | null {
  if (!(S > 0 && K > 0 && sigma > 0 && tLeftSec > 0)) return null;
  if (!Number.isFinite(S) || !Number.isFinite(K) || !Number.isFinite(sigma) || !Number.isFinite(tLeftSec)) return null;
  return Math.log(S / K) / (sigma * Math.sqrt(tLeftSec));
}

/** Analytic fair sensitivity: d(fair cents)/dS * $10 = 1000 * phi(d) / (S sigma sqrt t). */
export function fairCentsPer10(S: number, K: number, sigma: number, tLeftSec: number): number | null {
  const z = zScore(S, K, sigma, tLeftSec);
  if (z == null) return null;
  return (1000 * phiPdf(z)) / (S * sigma * Math.sqrt(tLeftSec));
}

const clip = (z: number) => (z > Z_CLIP ? Z_CLIP : z < -Z_CLIP ? -Z_CLIP : z);
const fin = (x: number | null | undefined): x is number => typeof x === "number" && Number.isFinite(x);
const round = (x: number, dp: number) => {
  const f = 10 ** dp;
  return Math.round(x * f) / f;
};

/**
 * Up mid usable for analysis: both upBid and upAsk present, not crossed, spread <= MAX_MID_SPREAD_CENTS.
 * (OddsSnap.upMid falls back to a single side when the other is empty, e.g. 3 or 97 in a thin book.)
 */
export function validUpMid(o: Pick<OddsSnap, "upBid" | "upAsk" | "upMid"> | null | undefined): number | null {
  if (!o) return null;
  const b = o.upBid;
  const a = o.upAsk;
  if (!fin(b) || !fin(a) || a < b || a - b > MAX_MID_SPREAD_CENTS) return null;
  return fin(o.upMid) ? o.upMid : (a + b) / 2;
}

// ---------------------------------------------------------------- per-window series

export interface WindowData {
  window: MarketWindow;
  /** 1s bars (60s seeded bars are ignored), ascending; a few seconds before the start help seed the fill */
  bars: Bar[];
  /** this window's odds rows, ascending */
  odds: OddsSnap[];
  /** this window's fair rows, ascending */
  fair: FairSnap[];
}

export interface WindowSeries {
  window: MarketWindow;
  /** epoch second of bucket 0 */
  startSec: number;
  n: number;
  btc: Float64Array;
  mid: Float64Array;
  /** seconds since the odds row behind mid[k] (0 = recorded in that second); NaN where mid[k] is NaN */
  midAge: Float64Array;
  sigma: Float64Array;
  /** recorded fair (cents) */
  fair: Float64Array;
  /** used where sigma[k] is missing: median recorded sigma, else realized vol of this window's 1s returns */
  sigmaFallback: number | null;
}

/**
 * Forward-filled 1s grid from (second, value) observations; later observations of a second win.
 * `ageOut` (optional, length n) receives the seconds since the observation behind each cell (NaN where empty).
 */
function fillGrid(
  n: number,
  startSec: number,
  maxFill: number,
  count: number,
  secOf: (i: number) => number,
  valOf: (i: number) => number | null | undefined,
  ageOut?: Float64Array,
): Float64Array {
  const raw = new Float64Array(n).fill(NaN);
  let seed = NaN;
  let seedSec = -Infinity;
  for (let i = 0; i < count; i++) {
    const v = valOf(i);
    if (!fin(v)) continue;
    const s = secOf(i);
    const idx = s - startSec;
    if (idx < 0) {
      if (s >= seedSec) {
        seed = v;
        seedSec = s;
      }
    } else if (idx < n) raw[idx] = v;
  }
  const out = new Float64Array(n).fill(NaN);
  ageOut?.fill(NaN);
  let last = seed;
  let lastIdx = seedSec - startSec;
  for (let i = 0; i < n; i++) {
    if (!Number.isNaN(raw[i]!)) {
      last = raw[i]!;
      lastIdx = i;
      out[i] = last;
      if (ageOut) ageOut[i] = 0;
    } else if (!Number.isNaN(last) && i - lastIdx <= maxFill) {
      out[i] = last;
      if (ageOut) ageOut[i] = i - lastIdx;
    }
  }
  return out;
}

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}

/** Build the 1s grid of one window from its recorded rows, up to (and including) the second of `untilTs`. */
export function buildSeries(d: WindowData, untilTs: number): WindowSeries | null {
  const w = d.window;
  const startSec = Math.floor(w.startTs / 1000);
  const lastSec = Math.floor((Math.min(w.endTs - 1, untilTs)) / 1000);
  const n = lastSec - startSec + 1;
  if (!(n > 1)) return null;
  const bars = d.bars.filter((b) => b.d === 1);
  const odds = d.odds.filter((o) => o.windowId === w.id);
  const fair = d.fair.filter((f) => f.windowId === w.id);
  const btc = fillGrid(n, startSec, BTC_FILL_SEC, bars.length, (i) => Math.floor(bars[i]!.t / 1000), (i) => {
    const c = bars[i]!.c;
    return c > 0 ? c : null;
  });
  const midAge = new Float64Array(n);
  const mid = fillGrid(
    n,
    startSec,
    ODDS_FILL_SEC,
    odds.length,
    (i) => Math.floor(odds[i]!.ts / 1000),
    (i) => validUpMid(odds[i]),
    midAge,
  );
  const secF = (i: number) => Math.floor(fair[i]!.ts / 1000);
  const sigma = fillGrid(n, startSec, FAIR_FILL_SEC, fair.length, secF, (i) => (fair[i]!.sigma > 0 ? fair[i]!.sigma : null));
  const fairC = fillGrid(n, startSec, FAIR_FILL_SEC, fair.length, secF, (i) => fair[i]!.fair);

  let sigmaFallback = median(fair.map((f) => f.sigma).filter((s) => fin(s) && s > 0));
  if (sigmaFallback == null) {
    let ss = 0;
    let cnt = 0;
    for (let k = 1; k < n; k++) {
      const a = btc[k - 1]!;
      const b = btc[k]!;
      if (a > 0 && b > 0) {
        const r = Math.log(b / a);
        ss += r * r;
        cnt++;
      }
    }
    sigmaFallback = cnt >= 60 && ss > 0 ? Math.sqrt(ss / cnt) : null;
  }
  return { window: w, startSec, n, btc, mid, midAge, sigma, fair: fairC, sigmaFallback };
}

/** mid[k] comes from an odds row at most ODDS_MAX_AGE_SEC old (false where there is no mid). */
const freshAge = (age: ArrayLike<number> | undefined, k: number): boolean => !age || age[k]! <= ODDS_MAX_AGE_SEC;

/** End-of-second time left (s) at bucket k: btc[k] / mid[k] are the values at the end of that second. */
const tLeftAt = (s: WindowSeries, k: number): number => (s.window.endTs - (s.startSec + k + 1) * 1000) / 1000;

const sigmaAt = (s: WindowSeries, k: number): number | null => {
  const v = s.sigma[k]!;
  return v > 0 ? v : s.sigmaFallback;
};

// ---------------------------------------------------------------- per-window partial (cacheable)

/** Compact scatter samples of one window. */
export interface ScatterSamples {
  len: number;
  ts: Float64Array;
  /** clipped z */
  z: Float32Array;
  mid: Float32Array;
  tLeft: Float32Array;
  /** upMid - 100*phi(unclipped z), cents */
  resid: Float32Array;
}

/** Everything the scope aggregates need from one window; immutable once the window is finished. */
export interface WindowPartial {
  windowId: string;
  startTs: number;
  /** seconds with an Up mid (0 = the window has no odds data) */
  samples: number;
  /** LAG_STEPS_SEC.length x [n, sx, sy, sxx, syy, sxy] */
  lag: Float64Array;
  scatter: ScatterSamples;
}

/**
 * Lag pairs: x = BTC 1s log return of second k (close k-1 -> close k); for lag L,
 * y = mid[k+L+1] - mid[k+L-1], the odds change over the 2s step that starts L seconds later.
 * A 2s step thus covers response delays L and L+1 s, so the 0,2,...,30 bars miss no delay.
 * With `midAge`, pairs whose odds endpoints are forward-filled beyond ODDS_MAX_AGE_SEC are dropped (a stalled
 * feed is no evidence that the odds did not move).
 */
export function lagAccumulate(
  btc: ArrayLike<number>,
  mid: ArrayLike<number>,
  acc?: Float64Array,
  midAge?: ArrayLike<number>,
): Float64Array {
  const out = acc ?? new Float64Array(LAG_STEPS_SEC.length * LAG_FIELDS);
  const n = Math.min(btc.length, mid.length);
  const r = new Float64Array(n).fill(NaN);
  for (let k = 1; k < n; k++) {
    const a = btc[k - 1]!;
    const b = btc[k]!;
    if (a > 0 && b > 0) r[k] = Math.log(b / a);
  }
  for (let li = 0; li < LAG_STEPS_SEC.length; li++) {
    const L = LAG_STEPS_SEC[li]!;
    const o = li * LAG_FIELDS;
    let cnt = 0, sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
    for (let k = 1; k + L + 1 < n; k++) {
      const x = r[k]!;
      const y = mid[k + L + 1]! - mid[k + L - 1]!;
      if (Number.isNaN(x) || Number.isNaN(y)) continue;
      if (!freshAge(midAge, k + L + 1) || !freshAge(midAge, k + L - 1)) continue;
      cnt++;
      sx += x;
      sy += y;
      sxx += x * x;
      syy += y * y;
      sxy += x * y;
    }
    out[o] = out[o]! + cnt;
    out[o + 1] = out[o + 1]! + sx;
    out[o + 2] = out[o + 2]! + sy;
    out[o + 3] = out[o + 3]! + sxx;
    out[o + 4] = out[o + 4]! + syy;
    out[o + 5] = out[o + 5]! + sxy;
  }
  return out;
}

/** Pearson rho from running sums; null below MIN_LAG_N pairs or with a flat series. */
export function pearson(n: number, sx: number, sy: number, sxx: number, syy: number, sxy: number, minN = MIN_LAG_N): number | null {
  if (n < minN) return null;
  const vx = sxx - (sx * sx) / n;
  const vy = syy - (sy * sy) / n;
  // relative guards: floating noise on a constant series must not read as variance
  if (!(vx > 1e-12 * Math.max(sxx, 1e-300)) || !(vy > 1e-12 * Math.max(syy, 1e-300))) return null;
  const rho = (sxy - (sx * sy) / n) / Math.sqrt(vx * vy);
  return Math.max(-1, Math.min(1, rho));
}

export function windowPartial(s: WindowSeries): WindowPartial {
  const w = s.window;
  let samples = 0;
  for (let k = 0; k < s.n; k++) if (!Number.isNaN(s.mid[k]!)) samples++;

  const lag = lagAccumulate(s.btc, s.mid, undefined, s.midAge);

  const cap = Math.ceil(s.n / SCATTER_STRIDE_SEC) + 1;
  const sc: ScatterSamples = {
    len: 0,
    ts: new Float64Array(cap),
    z: new Float32Array(cap),
    mid: new Float32Array(cap),
    tLeft: new Float32Array(cap),
    resid: new Float32Array(cap),
  };
  const K = w.priceToBeat;
  if (K != null && K > 0) {
    for (let k = 0; k < s.n && sc.len < cap; k++) {
      const sec = s.startSec + k;
      if (sec % SCATTER_STRIDE_SEC !== 0) continue;
      const S = s.btc[k]!;
      const M = s.mid[k]!;
      const sig = sigmaAt(s, k);
      if (Number.isNaN(S) || Number.isNaN(M) || sig == null || !freshAge(s.midAge, k)) continue;
      const ts = sec * 1000;
      const tLeft = tLeftAt(s, k);
      if (tLeft < MIN_TLEFT_SEC) continue;
      const z = zScore(S, K, sig, tLeft);
      if (z == null) continue;
      const i = sc.len++;
      sc.ts[i] = ts;
      sc.z[i] = clip(z);
      sc.mid[i] = M;
      sc.tLeft[i] = tLeft;
      sc.resid[i] = M - 100 * phi(z);
    }
  }
  return { windowId: w.id, startTs: w.startTs, samples, lag, scatter: sc };
}

// ---------------------------------------------------------------- aggregates

export function lagBars(partials: WindowPartial[]): { lag: LagBar[]; lagPeak: { lagSec: number; rho: number } | null } {
  const acc = new Float64Array(LAG_STEPS_SEC.length * LAG_FIELDS);
  for (const p of partials) for (let i = 0; i < acc.length; i++) acc[i] = acc[i]! + p.lag[i]!;
  const lag: LagBar[] = LAG_STEPS_SEC.map((lagSec, li) => {
    const o = li * LAG_FIELDS;
    const rho = pearson(acc[o]!, acc[o + 1]!, acc[o + 2]!, acc[o + 3]!, acc[o + 4]!, acc[o + 5]!);
    return { lagSec, rho: rho == null ? null : round(rho, 4), n: acc[o]! };
  });
  let lagPeak: { lagSec: number; rho: number } | null = null;
  for (const b of lag) if (b.rho != null && b.rho > 0 && (!lagPeak || b.rho > lagPeak.rho)) lagPeak = { lagSec: b.lagSec, rho: b.rho };
  return { lag, lagPeak };
}

/** Uniformly downsample the scope's scatter samples (time order kept) to at most `max` points. */
export function scatterPoints(partials: WindowPartial[], max: number): ScatterPoint[] {
  const ordered = [...partials].sort((a, b) => a.startTs - b.startTs);
  const total = ordered.reduce((n, p) => n + p.scatter.len, 0);
  const stride = Math.max(1, Math.ceil(total / Math.max(1, max)));
  const out: ScatterPoint[] = [];
  let g = 0;
  for (const p of ordered) {
    const s = p.scatter;
    for (let i = 0; i < s.len; i++, g++) {
      if (g % stride !== 0) continue;
      out.push({
        ts: s.ts[i]!,
        windowId: p.windowId,
        z: round(s.z[i]!, 3),
        upMid: round(s.mid[i]!, 2),
        tLeftSec: Math.round(s.tLeft[i]!),
      });
    }
  }
  return out;
}

/** Fair P(Up) vs z. In sigma*sqrt(t) units fair = 100*phi(z) for every time left, so this IS the curve for now. */
export function fairCurve(): { z: number; fair: number }[] {
  const out: { z: number; fair: number }[] = [];
  const steps = Math.round((2 * Z_CLIP) / FAIR_CURVE_STEP);
  for (let i = 0; i <= steps; i++) {
    const z = round(-Z_CLIP + i * FAIR_CURVE_STEP, 2);
    out.push({ z, fair: round(100 * phi(z), 3) });
  }
  return out;
}

/** Latest market state, preferred from the engine's live values over the recorded series. */
export interface LivePoint {
  ts: number;
  btc: number | null;
  upMid: number | null;
  sigma: number | null;
  /** fair cents as the engine computed it */
  fair: number | null;
}

export interface CurrentPoint {
  z: number;
  /** unclipped */
  zRaw: number;
  upMid: number;
  fair: number | null;
  tLeftSec: number;
}

export function currentPoint(
  win: MarketWindow | null,
  series: WindowSeries | null,
  live: LivePoint | null | undefined,
  now: number,
): CurrentPoint | null {
  if (!win) return null;
  const K = win.priceToBeat;
  if (K == null || !(K > 0)) return null;
  const tLeftSec = (win.endTs - now) / 1000;
  if (!(tLeftSec > 0) || now < win.startTs) return null;
  const make = (S: number, M: number, sig: number, fair: number | null): CurrentPoint | null => {
    const z = zScore(S, K, sig, tLeftSec);
    if (z == null) return null;
    return {
      z: round(clip(z), 3),
      zRaw: z,
      upMid: round(M, 2),
      fair: round(fin(fair) ? fair : 100 * phi(z), 3),
      tLeftSec: Math.round(tLeftSec),
    };
  };
  if (live && fin(live.btc) && fin(live.upMid) && now - live.ts <= CURRENT_MAX_AGE_MS) {
    const sig = fin(live.sigma) && live.sigma > 0 ? live.sigma : series?.sigmaFallback ?? null;
    if (sig != null) {
      const p = make(live.btc, live.upMid, sig, live.fair);
      if (p) return p;
    }
  }
  if (!series || series.window.id !== win.id) return null;
  for (let k = series.n - 1; k >= 0; k--) {
    if ((series.startSec + k) * 1000 < now - CURRENT_MAX_AGE_MS) break;
    const S = series.btc[k]!;
    const M = series.mid[k]!;
    const sig = sigmaAt(series, k);
    if (Number.isNaN(S) || Number.isNaN(M) || sig == null) continue;
    const f = series.fair[k]!;
    return make(S, M, sig, Number.isNaN(f) ? null : f);
  }
  return null;
}

/** BTC shift (s) for the sensitivity regression, from the lag peak. */
export function sensitivityShift(lagPeak: { lagSec: number; rho: number } | null): number {
  if (!lagPeak || lagPeak.rho < SENS_SHIFT_MIN_RHO) return 0;
  return Math.max(0, Math.min(SENS_MAX_SHIFT_SEC, Math.round(lagPeak.lagSec)));
}

/**
 * Cents of Up per $10 BTC through the window up to now.
 *   market: OLS slope of dUp on dBTC (SENS_HORIZON_SEC changes, every second) over the trailing SENS_WINDOW_SEC,
 *           pairing the odds change ending at j with the BTC change ending at j - shiftSec (the measured lag), x10;
 *           pairs whose odds endpoints are older than ODDS_MAX_AGE_SEC (stalled feed) are left out
 *   fair:   analytic dFair/dS x $10 averaged over the same trailing SENS_WINDOW_SEC (the slope the regression
 *           measures is an average over that window, not the derivative at its last second)
 * No points closer than SENS_MIN_TLEFT_SEC to the window end.
 */
export function sensitivitySeries(s: WindowSeries | null, shiftSec = 0): SensitivityPoint[] {
  if (!s) return [];
  const w = s.window;
  const K = w.priceToBeat;
  const h = SENS_HORIZON_SEC;
  const sh = Math.max(0, Math.floor(shiftSec));
  const n = s.n;
  // prefix sums over j of valid pairs
  const pc = new Float64Array(n + 1);
  const px = new Float64Array(n + 1);
  const py = new Float64Array(n + 1);
  const pxx = new Float64Array(n + 1);
  const pxy = new Float64Array(n + 1);
  // prefix sums of the point fair derivative (cents per $10)
  const pfc = new Float64Array(n + 1);
  const pf = new Float64Array(n + 1);
  for (let j = 0; j < n; j++) {
    let c = 0, x = 0, y = 0;
    const jb = j - sh;
    if (jb - h >= 0) {
      const dx = s.btc[jb]! - s.btc[jb - h]!;
      const dy = s.mid[j]! - s.mid[j - h]!;
      if (!Number.isNaN(dx) && !Number.isNaN(dy) && freshAge(s.midAge, j) && freshAge(s.midAge, j - h)) {
        c = 1;
        x = dx;
        y = dy;
      }
    }
    pc[j + 1] = pc[j]! + c;
    px[j + 1] = px[j]! + x;
    py[j + 1] = py[j]! + y;
    pxx[j + 1] = pxx[j]! + x * x;
    pxy[j + 1] = pxy[j]! + x * y;
    let fc = 0, fv = 0;
    const S = s.btc[j]!;
    const sig = sigmaAt(s, j);
    const tl = tLeftAt(s, j);
    if (K != null && K > 0 && !Number.isNaN(S) && sig != null && tl >= MIN_TLEFT_SEC) {
      const f = fairCentsPer10(S, K, sig, tl);
      if (f != null && Number.isFinite(f)) {
        fc = 1;
        fv = f;
      }
    }
    pfc[j + 1] = pfc[j]! + fc;
    pf[j + 1] = pf[j]! + fv;
  }
  const out: SensitivityPoint[] = [];
  for (let k = 0; k < n; k++) {
    const sec = s.startSec + k;
    if (sec % SENS_POINT_STEP_SEC !== 0 && k !== n - 1) continue;
    const ts = sec * 1000;
    const tLeftSec = (w.endTs - ts) / 1000;
    if (tLeftSec < SENS_MIN_TLEFT_SEC) continue;
    const lo = Math.max(0, k - SENS_WINDOW_SEC + 1);
    const cnt = pc[k + 1]! - pc[lo]!;
    let market: number | null = null;
    if (cnt >= SENS_MIN_N) {
      const sx = px[k + 1]! - px[lo]!;
      const sy = py[k + 1]! - py[lo]!;
      const sxx = pxx[k + 1]! - pxx[lo]!;
      const sxy = pxy[k + 1]! - pxy[lo]!;
      const vx = sxx / cnt - (sx / cnt) ** 2;
      if (vx >= SENS_MIN_VAR_X) market = round((10 * (sxy / cnt - (sx / cnt) * (sy / cnt))) / vx, 4);
    }
    const fCnt = pfc[k + 1]! - pfc[lo]!;
    const fair = fCnt > 0 ? round((pf[k + 1]! - pf[lo]!) / fCnt, 4) : null;
    out.push({ ts, tLeftSec: Math.round(tLeftSec), market, fair });
  }
  return out;
}

// ---------------------------------------------------------------- takeaways

const MINUS = "−";
const signed = (x: number, dp: number) => {
  const v = round(x, dp);
  if (v === 0) return (0).toFixed(dp);
  return (v > 0 ? "+" : MINUS) + Math.abs(v).toFixed(dp);
};
/** cents: one decimal under 10 (trailing .0 dropped), else whole */
export const fmtCents = (c: number) => {
  const a = Math.abs(c);
  const s = a < 10 ? String(round(a, 1)) : String(Math.round(a));
  return (c < 0 && round(a, 1) !== 0 ? MINUS : "") + s;
};

/** `live` false: scope "window" with nothing live, so its data is the last finished window. */
function scopeLabel(scope: AnalysisScope, nWindows: number, live = true): string {
  if (scope === "window") return live ? "this window" : "the last window";
  if (scope === "last20") return nWindows === 1 ? "the last window" : `the last ${nWindows} windows`;
  return nWindows === 1 ? "1 window" : `all ${nWindows} windows`;
}

export function scatterTakeaway(
  cur: CurrentPoint | null,
  partials: WindowPartial[],
  scope: AnalysisScope,
  nWindows: number,
  live = true,
): string {
  if (cur && cur.fair != null) {
    const where = Math.abs(cur.zRaw) > Z_CLIP ? `Beyond ${cur.zRaw > 0 ? "+" : MINUS}${Z_CLIP}σ` : `At ${signed(cur.zRaw, 1)}σ`;
    const diff = cur.upMid - cur.fair;
    if (Math.abs(diff) < 0.5) return `${where} the market sits right at fair.`;
    return `${where} the market sits ${fmtCents(Math.abs(diff))}¢ ${diff < 0 ? "under" : "over"} fair.`;
  }
  let aN = 0, aS = 0, bN = 0, bS = 0;
  for (const p of partials) {
    const s = p.scatter;
    for (let i = 0; i < s.len; i++) {
      const z = s.z[i]!;
      if (z > AT_STRIKE_Z) {
        aN++;
        aS += s.resid[i]!;
      } else if (z < -AT_STRIKE_Z) {
        bN++;
        bS += s.resid[i]!;
      }
    }
  }
  const side = (sum: number, cnt: number, where: string) => {
    const m = sum / cnt;
    return Math.abs(m) < 0.5 ? `at fair ${where}` : `${fmtCents(Math.abs(m))}¢ ${m < 0 ? "under" : "over"} fair ${where}`;
  };
  const above = aN >= MIN_SIDE_SAMPLES;
  const below = bN >= MIN_SIDE_SAMPLES;
  if (!above && !below) return NOT_ENOUGH;
  const text =
    above && below
      ? `${side(aS, aN, "above the strike")} and ${side(bS, bN, "below it")}`
      : above
        ? side(aS, aN, "above the strike")
        : side(bS, bN, "below the strike");
  return `Across ${scopeLabel(scope, nWindows, live)}, the market averages ${text}.`;
}

export function lagTakeaway(
  lag: LagBar[],
  peak: { lagSec: number; rho: number } | null,
  scope: AnalysisScope,
  nWindows: number,
  live = true,
): string {
  if (!lag.some((b) => b.rho != null)) return NOT_ENOUGH;
  const where = scopeLabel(scope, nWindows, live);
  if (!peak) return `Odds show no positive response to BTC moves within 30s over ${where}.`;
  const r = peak.rho.toFixed(2);
  if (peak.rho < 0.1) return `No clear lag: odds barely follow 1s BTC moves (peak ρ ${r}).`;
  if (peak.lagSec === 0) return `Odds react to BTC within ~1s (ρ ${r}), no measurable lag over ${where}.`;
  return `Odds trail BTC by ~${peak.lagSec}s (peak ρ ${r} over ${where}).`;
}

export function sensitivityTakeaway(points: SensitivityPoint[]): string {
  let m: number | null = null;
  let f: number | null = null;
  for (let i = points.length - 1; i >= 0 && (m == null || f == null); i--) {
    const p = points[i]!;
    if (m == null && p.market != null) m = p.market;
    if (f == null && p.fair != null) f = p.fair;
  }
  const fc = (x: number) => (Math.abs(x) < 10 ? Math.abs(x).toFixed(1) : String(Math.round(Math.abs(x))));
  const sc = (x: number) => (x < 0 && round(x, 1) !== 0 ? MINUS : "") + fc(x);
  if (m != null && f != null) {
    if (Math.abs(f) < 0.1 && Math.abs(m) < 0.1) return "Odds are pinned: market and fair both move under 0.1¢ per $10 BTC.";
    if (m < 0) return `The market moves against BTC: ${sc(m)}¢ per $10 vs ${fc(f)}¢ fair.`;
    const dev = f > 0 ? m / f - 1 : Infinity;
    if (Math.abs(dev) <= 0.15) return `The market moves in line with fair: ${fc(m)}¢ vs ${fc(f)}¢ per $10 BTC.`;
    if (dev < 0) return `The market under-reacts: ${fc(m)}¢ per $10 BTC vs ${fc(f)}¢ fair (${Math.round(-dev * 100)}% less).`;
    if (!Number.isFinite(dev)) return `The market moves ${fc(m)}¢ per $10 BTC while fair is flat.`;
    return `The market over-reacts: ${fc(m)}¢ per $10 BTC vs ${fc(f)}¢ fair (${Math.round(dev * 100)}% more).`;
  }
  if (f != null) return `Fair moves ${fc(f)}¢ per $10 BTC; too few BTC moves yet to measure the market.`;
  if (m != null) return `The market moves ${sc(m)}¢ per $10 BTC (fair unavailable).`;
  return NOT_ENOUGH;
}

// ---------------------------------------------------------------- assembly

export interface AssembleInput {
  scope: AnalysisScope;
  now: number;
  /** windows in scope (any order) */
  windows: MarketWindow[];
  /** partials of those windows (missing = no data) */
  partials: WindowPartial[];
  current: MarketWindow | null;
  /** current window's series up to now (sensitivity + "now" point) */
  currentSeries: WindowSeries | null;
  live?: LivePoint | null;
}

export function assembleAnalysis(i: AssembleInput): AnalysisPayload {
  const inScope = new Set(i.windows.map((w) => w.id));
  const partials = i.partials.filter((p) => inScope.has(p.windowId) && p.samples > 0);
  partials.sort((a, b) => a.startTs - b.startTs);
  const nWin = partials.length;
  // scope "window" with nothing live: the caller passed the last finished window; say so in the takeaways
  const live = !(i.scope === "window" && !i.current);

  const { lag, lagPeak } = lagBars(partials);
  const cur = currentPoint(i.current, i.currentSeries, i.live, i.now);
  const sensitivity = sensitivitySeries(
    i.currentSeries && i.current && i.currentSeries.window.id === i.current.id ? i.currentSeries : null,
    sensitivityShift(lagPeak),
  );
  return {
    scope: i.scope,
    computedTs: i.now,
    windowIds: partials.map((p) => p.windowId),
    scatter: scatterPoints(partials, MAX_SCATTER_POINTS[i.scope]),
    fairCurve: fairCurve(),
    current: cur ? { z: cur.z, upMid: cur.upMid, fair: cur.fair, tLeftSec: cur.tLeftSec } : null,
    lag,
    lagPeak,
    sensitivity,
    takeaways: {
      scatter: scatterTakeaway(cur, partials, i.scope, nWin, live),
      lag: lagTakeaway(lag, lagPeak, i.scope, nWin, live),
      sensitivity: !live && nWin > 0 ? NO_LIVE_WINDOW : sensitivityTakeaway(sensitivity),
    },
  };
}

/** One-shot analysis from flat arrays (the shared/analysis.ts AnalysisInput shape, plus an optional live point). */
export function computeAnalysis(input: AnalysisInput & { live?: LivePoint | null }): AnalysisPayload {
  const wins = [...input.windows].sort((a, b) => a.startTs - b.startTs);
  const byId = new Map<string, { odds: OddsSnap[]; fair: FairSnap[] }>();
  for (const w of wins) byId.set(w.id, { odds: [], fair: [] });
  for (const o of input.odds) byId.get(o.windowId)?.odds.push(o);
  for (const f of input.fair) byId.get(f.windowId)?.fair.push(f);
  const bars = input.bars.filter((b) => b.d === 1);
  const partials: WindowPartial[] = [];
  let currentSeries: WindowSeries | null = null;
  let lo = 0;
  for (const w of wins) {
    const from = w.startTs - BTC_FILL_SEC * 1000;
    while (lo < bars.length && bars[lo]!.t < from) lo++;
    let hi = lo;
    while (hi < bars.length && bars[hi]!.t < w.endTs) hi++;
    const rows = byId.get(w.id)!;
    const series = buildSeries({ window: w, bars: bars.slice(lo, hi), odds: rows.odds, fair: rows.fair }, input.now);
    if (!series) continue;
    partials.push(windowPartial(series));
    if (input.current && w.id === input.current.id) currentSeries = series;
  }
  if (input.current && !currentSeries) {
    // the current window may be outside the scope's list (not expected, but stay correct)
    const rows = {
      odds: input.odds.filter((o) => o.windowId === input.current!.id),
      fair: input.fair.filter((f) => f.windowId === input.current!.id),
    };
    currentSeries = buildSeries({ window: input.current, bars, ...rows }, input.now);
  }
  return assembleAnalysis({
    scope: input.scope,
    now: input.now,
    windows: wins,
    partials,
    current: input.current,
    currentSeries,
    live: input.live,
  });
}
