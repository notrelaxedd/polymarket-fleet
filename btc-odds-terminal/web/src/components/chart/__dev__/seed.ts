// DEV ONLY — synthetic data for working on the chart without a backend. MainChart never imports this.
//
//   import { seedChartDev } from "@/components/chart/__dev__/seed";
//   const stop = seedChartDev();          // fills the store + starts a live ticker (call stop() to end)
//
// Generates: 2h of seeded 60s bars, then `hours` of 1s GBM bars around 67k with raw ticks carrying
// per-exchange sources; 15m windows with PTBs and settlements; Up odds that track
// Φ(ln(S/K)/(σ√t)) with a few seconds of lag + noise; fair snaps with edge. The live ticker mutates the
// arrays in place and bumps `version`, exactly like the ingest layer, and rolls windows over.

import type { Bar, BtcTick, FairSnap, FeedStatus, MarketWindow, OddsSnap, Settings } from "@shared/protocol";
import { useTerminal } from "@/lib/store";

const WIN = 15 * 60 * 1000;
const SIGMA = 0.00009; // per sqrt(second)
const LAG_S = 4;

export interface SeedOptions {
  hours?: number;
  seedHours60?: number;
  live?: boolean;
  startPrice?: number;
  seed?: number;
}

function mulberry32(a: number) {
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function normCdf(x: number) {
  // Abramowitz–Stegun 7.1.26
  const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
  const y =
    1 -
    ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) *
      t *
      Math.exp(-(x * x) / 2);
  return x >= 0 ? (1 + y) / 2 : (1 - y) / 2;
}

const winId = (start: number) => `btc-updown-15m-${Math.floor(start / 1000)}`;
const r2 = (v: number) => Math.round(v * 100) / 100;

function makeWindow(start: number, status: MarketWindow["status"], ptb: number | null): MarketWindow {
  const id = winId(start);
  return {
    id,
    title: "BTC Up or Down · 15m",
    startTs: start,
    endTs: start + WIN,
    priceToBeat: ptb,
    upInstrument: `${id}:up`,
    downInstrument: `${id}:down`,
    status,
    result: null,
    settlePrice: null,
  };
}

export function seedChartDev(opts: SeedOptions = {}): () => void {
  const hours = opts.hours ?? 3;
  const seedHours60 = opts.seedHours60 ?? 2;
  const rnd = mulberry32(opts.seed ?? 7);
  const randn = () => {
    const u = Math.max(rnd(), 1e-12);
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rnd());
  };

  const now = Date.now();
  const nowSec = Math.floor(now / 1000) * 1000;
  const start1s = Math.floor((now - hours * 3600_000) / 60_000) * 60_000;
  const start60 = start1s - seedHours60 * 3600_000;
  let S = opts.startPrice ?? 67_000;
  const step = (dtSec: number) => {
    S *= Math.exp(SIGMA * Math.sqrt(dtSec) * randn() - 0.5 * SIGMA * SIGMA * dtSec);
    return S;
  };

  const bars: Bar[] = [];
  const ticks: BtcTick[] = [];
  const odds: OddsSnap[] = [];
  const fair: FairSnap[] = [];
  const priceAt = new Map<number, number>(); // window start/end -> price

  // seeded 60s history
  for (let t = start60; t < start1s; t += 60_000) {
    if (t % WIN === 0) priceAt.set(t, S);
    const o = S;
    let h = o;
    let l = o;
    for (let k = 0; k < 30; k++) {
      step(2);
      h = Math.max(h, S);
      l = Math.min(l, S);
    }
    bars.push({ t, d: 60, o: r2(o), h: r2(h), l: r2(l), c: r2(S) });
  }

  const sources = (p: number) => ({
    coinbase: r2(p + 1.2 + randn() * 1.8),
    kraken: r2(p - 1.8 + randn() * 2.6),
    bitstamp: r2(p + 0.4 + randn() * 2.2),
  });

  // odds model state
  const fairHist: number[] = [];
  let noise = 0;
  let bias = 0;
  const oddsFor = (ts: number, w: MarketWindow, price: number) => {
    const tLeft = Math.max(0.5, (w.endTs - ts) / 1000);
    const K = w.priceToBeat ?? price;
    const fv = 100 * normCdf(Math.log(price / K) / (SIGMA * Math.sqrt(tLeft)));
    fairHist.push(fv);
    if (fairHist.length > 32) fairHist.shift();
    const lagged = fairHist[Math.max(0, fairHist.length - 1 - LAG_S)];
    noise = noise * 0.9 + randn() * 0.7;
    bias = bias * 0.995 + randn() * 0.08;
    const mkt = Math.min(98.5, Math.max(1.5, lagged + noise + bias * 2));
    const spread = mkt > 10 && mkt < 90 ? (rnd() < 0.35 ? 3 : 2) : 1;
    const upBid = Math.max(1, Math.min(98, Math.round(mkt - spread / 2)));
    const upAsk = Math.min(99, upBid + spread);
    const upMid = (upBid + upAsk) / 2;
    const o: OddsSnap = { ts, windowId: w.id, upBid, upAsk, downBid: 100 - upAsk, downAsk: 100 - upBid, upMid };
    const f: FairSnap = {
      ts,
      windowId: w.id,
      fair: Math.round(fv * 10) / 10,
      sigma: SIGMA,
      tLeftSec: Math.round(tLeft),
      edge: Math.round((fv - upMid) * 10) / 10,
    };
    return { o, f };
  };

  // windows
  const windows: Record<string, MarketWindow> = {};
  const firstWin = Math.ceil(start60 / WIN) * WIN;
  const curStart = Math.floor(now / WIN) * WIN;

  // 1s live-recorded history
  let w: MarketWindow | null = null;
  for (let t = start1s; t <= nowSec; t += 1000) {
    const ws = Math.floor(t / WIN) * WIN;
    if (t % WIN === 0) priceAt.set(t, S);
    if (!w || w.startTs !== ws) {
      w = makeWindow(ws, ws === curStart ? "live" : "settled", r2(priceAt.get(ws) ?? S));
      windows[w.id] = w;
      fairHist.length = 0;
    }
    const o = S;
    let h = o;
    let l = o;
    for (let k = 0; k < 4; k++) {
      step(0.25);
      h = Math.max(h, S);
      l = Math.min(l, S);
    }
    bars.push({ t, d: 1, o: r2(o), h: r2(h), l: r2(l), c: r2(S) });
    ticks.push({ ts: t + 900, price: r2(S), sources: sources(S) });
    const { o: os, f } = oddsFor(t + 950, w, S);
    odds.push(os);
    fair.push(f);
  }
  // windows fully inside the 60s seed region (no odds recorded for them)
  for (let ws = firstWin; ws < start1s; ws += WIN) {
    if (windows[winId(ws)]) continue;
    const ww = makeWindow(ws, "settled", r2(priceAt.get(ws) ?? S));
    windows[ww.id] = ww;
  }
  // settle finished windows
  const sorted = Object.values(windows).sort((a, b) => a.startTs - b.startTs);
  for (const x of sorted) {
    if (x.endTs > now) continue;
    const end = priceAt.get(x.endTs) ?? closeAt(bars, x.endTs);
    x.settlePrice = r2(end);
    x.result = end >= (x.priceToBeat ?? end) ? "up" : "down";
    x.status = "settled";
  }
  const current = windows[winId(curStart)];
  const next = makeWindow(curStart + WIN, "upcoming", null);
  windows[next.id] = next;
  const recent = sorted.filter((x) => x.endTs <= now).sort((a, b) => b.startTs - a.startTs);

  const settings: Settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "dev seed" };
  const feeds = (ts: number): FeedStatus[] =>
    (["coinbase", "kraken", "bitstamp", "polymarket"] as const).map((id) => ({ id, state: "live", lastMsgTs: ts, ageMs: 120 }));

  const store = useTerminal;
  store.getState().applySnapshot({
    serverTime: Date.now(),
    settings,
    current,
    next,
    recent,
    lastTick: ticks[ticks.length - 1],
    lastOdds: odds[odds.length - 1],
    lastFair: fair[fair.length - 1],
    feeds: feeds(now),
  });
  store.setState({ windows: { ...windows }, connected: true });
  store.getState().setHistory({ bars, ticks, odds, fair });

  if (opts.live === false) return () => {};

  // ---- live ticker (mimics ingest: in-place mutation + one version bump per flush) -------------------
  let lastOddsSec = Math.floor(nowSec / 1000);
  const id = setInterval(() => {
    const st = store.getState();
    const ser = st.series;
    const t = Date.now();
    step(0.25);
    const price = r2(S);
    const tick: BtcTick = { ts: t, price, sources: sources(S) };
    ser.ticks.push(tick);
    const bucket = Math.floor(t / 1000) * 1000;
    const last = ser.bars[ser.bars.length - 1];
    if (last && last.d === 1 && last.t === bucket) {
      ser.bars[ser.bars.length - 1] = { t: bucket, d: 1, o: last.o, h: Math.max(last.h, price), l: Math.min(last.l, price), c: price };
    } else if (!last || bucket > last.t) {
      const o = last ? last.c : price;
      ser.bars.push({ t: bucket, d: 1, o, h: Math.max(o, price), l: Math.min(o, price), c: price });
    }

    const patch: Partial<ReturnType<typeof store.getState>> = { lastTick: tick, feeds: feeds(t) };
    let cur = st.current;
    // rollover
    if (cur && t >= cur.endTs) {
      const done: MarketWindow = {
        ...cur,
        status: "settled",
        settlePrice: price,
        result: price >= (cur.priceToBeat ?? price) ? "up" : "down",
      };
      const nw: MarketWindow = { ...(st.next ?? makeWindow(cur.endTs, "upcoming", null)), status: "live", priceToBeat: price };
      const nn = makeWindow(nw.endTs, "upcoming", null);
      patch.current = nw;
      patch.next = nn;
      patch.recent = [done, ...st.recent].slice(0, 50);
      patch.windows = { ...st.windows, [done.id]: done, [nw.id]: nw, [nn.id]: nn };
      cur = nw;
      fairHist.length = 0;
    }
    const sec = Math.floor(t / 1000);
    if (cur && sec !== lastOddsSec) {
      lastOddsSec = sec;
      const { o, f } = oddsFor(t, cur, S);
      ser.odds.push(o);
      ser.fair.push(f);
      patch.lastOdds = o;
      patch.lastFair = f;
    }
    patch.version = st.version + 1;
    store.setState(patch);
  }, 250);

  return () => clearInterval(id);
}

function closeAt(bars: Bar[], ts: number): number {
  let lo = 0;
  let hi = bars.length - 1;
  let ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].t < ts) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return bars[ans]?.c ?? 0;
}
