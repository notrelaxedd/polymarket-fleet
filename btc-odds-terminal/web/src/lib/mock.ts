import type {
  Bar,
  BookLevel,
  BookLevels,
  BtcTick,
  ExchangeId,
  FairSnap,
  FeedId,
  FeedStatus,
  MarketWindow,
  OddsSnap,
  ServerMsg,
  Settings,
  SettingsPatch,
  StateSnapshot,
} from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { createMockModules, type MockCoreCtx } from "./mock/index";
import { setMockRoutes } from "./mock/router";
import type { MockCtx, MockModule, MockModules } from "./mock/types";

/**
 * Synthetic backend used when NEXT_PUBLIC_MOCK=1 or ?mock=1. It generates 3h of 1s history and then
 * streams live ServerMsgs at ~4/s through the same ingest path as the real WebSocket.
 *   - BTC: GBM, sigma ~0.00012/sqrt(s), around 67k. Per-exchange prices = proxy +/- up to $5; proxy = median.
 *   - Windows: 15-minute, wall-clock aligned, PTB = proxy price at window start.
 *   - Up odds: track fair = Phi(ln(S/K)/(sigma*sqrt(t))) with 1-4s lag, AR(1) noise, 1-3c spread.
 *   - Feeds: occasionally one exchange goes stale for ~8s. `?stale=polymarket` (or an exchange) forces one stale.
 *   - Up book: 5 levels a side around the quote, sizes 40-900 (emitted as `book` every second).
 *   - Steps 3-6 features live in lib/mock/* modules (trading, risk, live, alerts, drawings, analysis, replay):
 *     built once with a MockCtx, driven by onStep / onSecond / onRollover, merged into the snapshot, and
 *     serving every REST call api.ts makes in mock mode (lib/mock/router.ts).
 */

const SIGMA = 0.00012; // per sqrt(second)
const STEP_MS = 250;
const WIN_MS = 15 * 60 * 1000;
const HISTORY_MS = 3 * 3600 * 1000;
const EXCHANGES: ExchangeId[] = ["coinbase", "kraken", "bitstamp"];

function randn(): number {
  let u = 0;
  let v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function erf(x: number): number {
  const s = Math.sign(x);
  x = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * x);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return s * y;
}
export const normCdf = (z: number) => 0.5 * (1 + erf(z / Math.SQRT2));

const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

type Emit = (m: ServerMsg) => void;

class MockBackend {
  private bars_: Bar[] = [];
  private ticks_: BtcTick[] = [];
  private odds_: OddsSnap[] = [];
  private fair_: FairSnap[] = [];
  private wins = new Map<string, MarketWindow>();
  private settings_: Settings = {
    mode: "paper",
    volLookbackMin: 30,
    feeCentsPerContract: null,
    feeNote: "Fee schedule unconfirmed (mock)",
    risk: { ...DEFAULT_RISK_LIMITS },
    // mock "live" is simulated (no exchange) so the live toggle + confirm dialog are demoable
    liveAvailable: true,
  };
  private book_: BookLevels | null = null;
  private mods: MockModules | null = null;
  private modList: MockModule[] = [];

  private secClose: number[] = []; // proxy price per second, index = sec - baseSec
  private baseSec = 0;
  private S = 67000;
  private nowMs = 0; // simulated clock, advances in STEP_MS increments
  private noise = 0;
  private lag = 2;
  private spread = 2;
  private nextSpreadAt = 0;
  private current!: MarketWindow;
  private next!: MarketWindow;
  private timer: ReturnType<typeof setInterval> | null = null;
  private subs = new Set<Emit>();
  private staleUntil: Partial<Record<FeedId, number>> = {};
  private staleSince: Partial<Record<FeedId, number>> = {};
  private nextStaleAt = 0;
  private lastMsg: Record<FeedId, number> = { coinbase: 0, kraken: 0, bitstamp: 0, polymarket: 0 };
  private forceStale: FeedId | null = null;
  private lastTick: BtcTick | null = null;
  private lastOdds: OddsSnap | null = null;
  private lastFair: FairSnap | null = null;

  constructor() {
    const now = Math.floor(Date.now() / STEP_MS) * STEP_MS;
    const pathStart = Math.ceil((now - HISTORY_MS - WIN_MS) / 1000) * 1000;
    this.baseSec = pathStart / 1000;
    this.nowMs = pathStart;
    this.S = 67000 + randn() * 150;
    try {
      const f = new URLSearchParams(window.location.search).get("stale");
      if (f === "polymarket" || EXCHANGES.includes(f as ExchangeId)) this.forceStale = f as FeedId;
    } catch {}
    this.nextStaleAt = now + 25_000;
    // history
    while (this.nowMs < now) this.advance(false);
    this.lastMsg = { coinbase: now, kraken: now, bitstamp: now, polymarket: now };
  }

  // ---- path / windows ----
  private priceAt(ms: number): number {
    const i = Math.floor(ms / 1000) - this.baseSec;
    return this.secClose[clamp(i, 0, this.secClose.length - 1)] ?? this.S;
  }

  private windowFor(startTs: number): MarketWindow {
    const id = `btc-updown-15m-${startTs / 1000}`;
    let w = this.wins.get(id);
    if (!w) {
      const known = startTs <= this.nowMs;
      w = {
        id,
        title: "Bitcoin Up or Down - 15 min",
        startTs,
        endTs: startTs + WIN_MS,
        priceToBeat: known ? this.priceAt(startTs) : null,
        upInstrument: `${id}:up`,
        downInstrument: `${id}:down`,
        status: known ? "live" : "upcoming",
        result: null,
        settlePrice: null,
      };
      this.wins.set(id, w);
    }
    return w;
  }

  private settle(w: MarketWindow): MarketWindow {
    const settlePrice = this.priceAt(w.endTs);
    const s: MarketWindow = {
      ...w,
      status: "settled",
      settlePrice: Math.round(settlePrice * 100) / 100,
      result: settlePrice >= (w.priceToBeat ?? settlePrice) ? "up" : "down",
    };
    this.wins.set(s.id, s);
    return s;
  }

  // ---- one simulation step (250ms) ----
  private advance(live: boolean): { tick: BtcTick; secondBoundary: boolean } {
    const dt = STEP_MS / 1000;
    this.nowMs += STEP_MS;
    const ts = this.nowMs;
    this.S *= Math.exp(SIGMA * Math.sqrt(dt) * randn());
    this.S = clamp(this.S, 55000, 80000);

    const sources: Partial<Record<ExchangeId, number>> = {};
    const vals: number[] = [];
    for (const ex of EXCHANGES) {
      const staleEnd = this.staleUntil[ex] ?? 0;
      const stale = live && (ts < staleEnd || this.forceStale === ex);
      if (stale) continue;
      const p = Math.round((this.S + clamp(randn() * 2.5, -5, 5)) * 100) / 100;
      sources[ex] = p;
      vals.push(p);
      this.lastMsg[ex] = ts;
    }
    if (!vals.length) vals.push(this.S);
    vals.sort((a, b) => a - b);
    const mid = vals.length % 2 ? vals[(vals.length - 1) / 2] : (vals[vals.length / 2 - 1] + vals[vals.length / 2]) / 2;
    const price = Math.round(mid * 100) / 100;
    const tick: BtcTick = { ts, price, sources };

    // windows roll on the simulated clock
    const sec = Math.floor(ts / 1000);
    const idx = sec - this.baseSec;
    this.secClose[idx] = price;
    const secondBoundary = ts % 1000 === 0;

    // bars/ticks bookkeeping (1s OHLC; one tick per second kept as raw history)
    const bucket = sec * 1000 - (secondBoundary ? 1000 : 0);
    const bsec = Math.floor(bucket / 1000);
    const last = this.bars_[this.bars_.length - 1];
    if (last && last.t === bsec * 1000) {
      last.h = Math.max(last.h, price);
      last.l = Math.min(last.l, price);
      last.c = price;
    } else {
      const o = last ? last.c : price;
      this.bars_.push({ t: bsec * 1000, d: 1, o, h: Math.max(o, price), l: Math.min(o, price), c: price });
    }
    if (secondBoundary) this.ticks_.push(tick);
    this.lastTick = tick;
    return { tick, secondBoundary };
  }

  /** odds + fair for the second ending at ts (uses lagged proxy price) */
  private computeOdds(ts: number, w: MarketWindow, price: number): { odds: OddsSnap; fair: FairSnap } {
    const K = w.priceToBeat ?? price;
    const tLeft = Math.max(0.5, (w.endTs - ts) / 1000);
    const sigma = SIGMA * (1 + 0.08 * Math.sin(ts / 90_000));
    const fairP = clamp(100 * normCdf(Math.log(price / K) / (sigma * Math.sqrt(tLeft))), 1, 99);
    // market follows fair with a lag, AR(1) noise, and a mild pull toward 50
    if (Math.random() < 0.05) this.lag = clamp(this.lag + (Math.random() < 0.5 ? -1 : 1), 1, 4);
    const lagged = this.priceAt(ts - this.lag * 1000);
    const lagFair = clamp(100 * normCdf(Math.log(lagged / K) / (sigma * Math.sqrt(tLeft + this.lag))), 1, 99);
    this.noise = 0.9 * this.noise + 1.0 * randn();
    const marketMid = clamp(lagFair * 0.97 + 50 * 0.03 + this.noise * 0.9, 2, 98);
    if (ts >= this.nextSpreadAt) {
      this.spread = Math.random() < 0.55 ? 2 : Math.random() < 0.5 ? 1 : 3;
      this.nextSpreadAt = ts + 4000 + Math.random() * 6000;
    }
    const bid = clamp(Math.round(marketMid - this.spread / 2), 1, 98 - this.spread + 1);
    const ask = Math.min(99, bid + this.spread);
    const mid = (bid + ask) / 2;
    const odds: OddsSnap = { ts, windowId: w.id, upBid: bid, upAsk: ask, downBid: 100 - ask, downAsk: 100 - bid, upMid: mid };
    const fair: FairSnap = {
      ts,
      windowId: w.id,
      fair: Math.round(fairP * 100) / 100,
      sigma,
      tLeftSec: tLeft,
      edge: Math.round((fairP - mid) * 100) / 100,
    };
    return { odds, fair };
  }

  // builds all history (called once from constructor after the path exists)
  private buildHistory() {
    const now = this.nowMs;
    const firstWin = Math.ceil((this.baseSec * 1000) / WIN_MS) * WIN_MS;
    for (let s = firstWin; s <= now; s += WIN_MS) {
      const w = this.windowFor(s);
      if (w.endTs <= now) this.settle(w);
    }
    const curStart = Math.floor(now / WIN_MS) * WIN_MS;
    this.current = this.windowFor(curStart);
    this.next = this.windowFor(curStart + WIN_MS);
    // per-second odds/fair for the last HISTORY_MS
    const from = now - HISTORY_MS;
    for (let t = Math.ceil(from / 1000) * 1000; t <= now; t += 1000) {
      const start = Math.floor(t / WIN_MS) * WIN_MS;
      const w = this.wins.get(`btc-updown-15m-${start / 1000}`);
      if (!w || w.priceToBeat == null) continue;
      const { odds, fair } = this.computeOdds(t, w, this.priceAt(t));
      this.odds_.push(odds);
      this.fair_.push(fair);
      this.lastOdds = odds;
      this.lastFair = fair;
    }
  }

  private init = false;
  ensureBuilt() {
    if (this.init) return;
    this.init = true;
    this.buildHistory();
    if (this.lastOdds) this.book_ = this.makeBook(this.lastOdds);
    const core: MockCoreCtx = {
      emit: (m) => this.emit(m),
      now: () => this.nowMs,
      current: () => this.wins.get(this.current.id) ?? this.current,
      next: () => this.wins.get(this.next.id) ?? this.next,
      getWindow: (id) => this.wins.get(id) ?? null,
      windows: (limit) => this.windows(limit),
      lastTick: () => this.lastTick,
      lastOdds: () => this.lastOdds,
      lastFair: () => this.lastFair,
      book: () => this.book_,
      settings: () => this.settings(),
      putSettings: (p) => this.putSettings(p),
      bars: (f, t) => this.bars(f, t),
      odds: (f, t) => this.odds(f, t),
      fair: (f, t) => this.fair(f, t),
      ticks: (f, t) => this.ticks(f, t),
      priceAt: (ms) => this.priceAt(ms),
    };
    const { modules, list } = createMockModules(core);
    this.mods = modules;
    this.modList = list;
  }

  /** the feature modules (trading, risk, live, alerts, drawings, analysis, replay) */
  modules(): MockModules {
    this.ensureBuilt();
    return this.mods!;
  }

  private hook(f: (m: MockModule) => void) {
    for (const m of this.modList) {
      try {
        f(m);
      } catch (err) {
        console.error(`mock module ${m.name} failed`, err);
      }
    }
  }

  /** Up book around the quote: 5 levels a side, 1c apart, sizes 40-900, best first. */
  private makeBook(o: OddsSnap): BookLevels | null {
    if (o.upBid == null || o.upAsk == null) return null;
    const size = () => Math.round(40 + Math.random() * Math.random() * 860);
    const bids: BookLevel[] = [];
    const asks: BookLevel[] = [];
    for (let i = 0; i < 5; i++) {
      if (o.upBid - i >= 1) bids.push({ price: o.upBid - i, size: size() });
      if (o.upAsk + i <= 99) asks.push({ price: o.upAsk + i, size: size() });
    }
    const w = this.wins.get(o.windowId);
    return { ts: o.ts, windowId: o.windowId, instrument: w?.upInstrument ?? `${o.windowId}:up`, bids, asks, state: "MARKET_STATE_OPEN" };
  }

  // ---- REST-like accessors ----
  private slice<T>(arr: T[], ts: (x: T) => number, from: number, to: number): T[] {
    return arr.filter((x) => ts(x) >= from && ts(x) <= to);
  }
  bars(from: number, to: number): Bar[] {
    this.ensureBuilt();
    return this.slice(this.bars_, (x) => x.t, from, to).map((b) => ({ ...b }));
  }
  odds(from: number, to: number): OddsSnap[] {
    this.ensureBuilt();
    return this.slice(this.odds_, (x) => x.ts, from, to).map((x) => ({ ...x }));
  }
  fair(from: number, to: number): FairSnap[] {
    this.ensureBuilt();
    return this.slice(this.fair_, (x) => x.ts, from, to).map((x) => ({ ...x }));
  }
  /** seeded ticks (1/s) for per-exchange lines */
  ticks(from: number, to: number): BtcTick[] {
    this.ensureBuilt();
    return this.slice(this.ticks_, (x) => x.ts, from, to);
  }
  windows(limit: number): MarketWindow[] {
    this.ensureBuilt();
    return [...this.wins.values()].sort((a, b) => b.startTs - a.startTs).slice(0, limit);
  }
  settings(): Settings {
    return { ...this.settings_, risk: { ...(this.settings_.risk ?? DEFAULT_RISK_LIMITS) } };
  }
  putSettings(p: SettingsPatch & Partial<Pick<Settings, "feeNote" | "liveAvailable">>): Settings {
    const { risk, feeNote: _n, liveAvailable: _l, ...rest } = p;
    this.settings_ = { ...this.settings_, ...rest, risk: { ...(this.settings_.risk ?? DEFAULT_RISK_LIMITS), ...(risk ?? {}) } };
    const s = this.settings();
    this.subs.forEach((e) => e({ type: "settings", data: s }));
    this.hook((m) => m.onSettings?.(s));
    return this.settings();
  }

  private recent(): MarketWindow[] {
    return [...this.wins.values()]
      .filter((w) => w.status === "settled")
      .sort((a, b) => b.startTs - a.startTs)
      .slice(0, 12);
  }

  private feeds(ts: number): FeedStatus[] {
    const ids: FeedId[] = ["coinbase", "kraken", "bitstamp", "polymarket"];
    return ids.map((id) => {
      const forced = this.forceStale === id;
      const staleEnd = this.staleUntil[id] ?? 0;
      if (forced) {
        const age = 6000 + (ts % 4000);
        return { id, state: "stale" as const, lastMsgTs: ts - age, ageMs: age };
      }
      if (ts < staleEnd) {
        const age = ts - (this.staleSince[id] ?? ts);
        return { id, state: age > 3000 ? ("stale" as const) : ("live" as const), lastMsgTs: ts - age, ageMs: age };
      }
      const age = Math.round(60 + Math.random() * 700);
      return { id, state: "live" as const, lastMsgTs: ts - age, ageMs: age };
    });
  }

  snapshot(): StateSnapshot {
    this.ensureBuilt();
    const extra: Partial<StateSnapshot> = {};
    this.hook((m) => Object.assign(extra, m.snapshot?.()));
    return {
      ...extra,
      book: this.book_,
      serverTime: Date.now(),
      settings: this.settings(),
      current: this.wins.get(this.current.id) ?? this.current,
      next: this.wins.get(this.next.id) ?? this.next,
      recent: this.recent(),
      lastTick: this.lastTick,
      lastOdds: this.lastOdds,
      lastFair: this.lastFair,
      feeds: this.feeds(this.nowMs),
    };
  }

  // ---- live stream ----
  subscribe(emit: Emit): () => void {
    this.ensureBuilt();
    this.subs.add(emit);
    if (!this.timer) this.run();
    return () => {
      this.subs.delete(emit);
      if (!this.subs.size && this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    };
  }

  private emit(m: ServerMsg) {
    this.subs.forEach((e) => e(m));
  }

  private run() {
    this.timer = setInterval(() => {
      // catch up to wall clock (timers can be throttled in background tabs)
      const target = Math.floor(Date.now() / STEP_MS) * STEP_MS;
      let guard = 0;
      while (this.nowMs < target && guard++ < 400) {
        const { tick, secondBoundary } = this.advance(true);
        const ts = this.nowMs;
        this.emit({ type: "btc", data: tick });
        this.hook((m) => m.onStep?.(ts));

        // window boundary
        if (ts >= this.current.endTs) {
          const settled = this.settle(this.current);
          const started = this.windowFor(this.next.startTs);
          const startedLive: MarketWindow = { ...started, priceToBeat: this.priceAt(started.startTs), status: "live" };
          this.wins.set(startedLive.id, startedLive);
          const upcoming = this.windowFor(startedLive.startTs + WIN_MS);
          this.current = startedLive;
          this.next = upcoming;
          this.emit({ type: "window", data: { ...settled, status: "closed", result: null, settlePrice: null } });
          this.emit({ type: "rollover", data: { current: startedLive, next: upcoming } });
          this.book_ = null;
          this.hook((m) => m.onRollover?.(settled, startedLive));
          setTimeout(() => this.emit({ type: "window", data: settled }), 2500);
        }

        if (secondBoundary) {
          const w = this.wins.get(this.current.id)!;
          const { odds, fair } = this.computeOdds(ts, w, tick.price);
          this.odds_.push(odds);
          this.fair_.push(fair);
          this.lastOdds = odds;
          this.lastFair = fair;
          this.emit({ type: "odds", data: odds });
          this.emit({ type: "fair", data: fair });
          this.book_ = this.makeBook(odds);
          if (this.book_) this.emit({ type: "book", data: this.book_ });
          this.hook((m) => m.onSecond?.(ts));
          this.lastMsg.polymarket = ts;
          // feed health
          if (ts >= this.nextStaleAt && !this.forceStale) {
            const ex = EXCHANGES[Math.floor(Math.random() * EXCHANGES.length)];
            this.staleSince[ex] = ts;
            this.staleUntil[ex] = ts + 6000 + Math.random() * 5000;
            this.nextStaleAt = ts + 40_000 + Math.random() * 50_000;
          }
          this.emit({ type: "feeds", data: this.feeds(ts) });
        }
      }
      if (this.nowMs < target - 5000) this.nowMs = target; // way behind: skip ahead
    }, STEP_MS);
  }
}

let inst: MockBackend | null = null;
export function getMock(): MockBackend {
  if (!inst) {
    inst = new MockBackend();
    inst.ensureBuilt();
  }
  return inst;
}
