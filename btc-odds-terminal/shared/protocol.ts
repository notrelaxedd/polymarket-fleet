// Wire contract between the worker backend (Fastify + ws) and the Next.js frontend.
// Every timestamp is epoch milliseconds UTC. Every contract price is in CENTS (0-100),
// never dollars. BTC prices are USD.

export type ExchangeId = "coinbase" | "kraken" | "bitstamp";
export type FeedId = ExchangeId | "polymarket";
export type FeedState = "live" | "stale" | "down";

/** One feed's health. stale = no message for > STALE_AFTER_MS; down = socket closed / never connected. */
export interface FeedStatus {
  id: FeedId;
  state: FeedState;
  lastMsgTs: number | null;
  /** ms since the last message at the time this status was produced */
  ageMs: number | null;
  detail?: string;
}

export const STALE_AFTER_MS = 3000;

/** One BRTI-proxy update: median of the live exchanges' last trade/mid. */
export interface BtcTick {
  ts: number;
  price: number;
  /** last price per exchange that contributed (live only) */
  sources: Partial<Record<ExchangeId, number>>;
}

/** OHLC bar of the BRTI proxy. `d` is the bar duration in seconds (1 for live bars, 60 for seeded history). */
export interface Bar {
  t: number; // bar open time, ms
  d: number;
  o: number;
  h: number;
  l: number;
  c: number;
}

/** Top-of-book for the current window's Up and Down instruments, in cents. */
export interface OddsSnap {
  ts: number;
  windowId: string;
  upBid: number | null;
  upAsk: number | null;
  downBid: number | null;
  downAsk: number | null;
  /** (upBid+upAsk)/2 when both exist, else whichever exists, else null */
  upMid: number | null;
}

export interface FairSnap {
  ts: number;
  windowId: string;
  /** P(Up) in cents */
  fair: number;
  /** realized vol per sqrt(second), fractional log-return units */
  sigma: number;
  tLeftSec: number;
  /** fair - upMid, cents; null when no mid */
  edge: number | null;
}

export type WindowStatus = "upcoming" | "live" | "closed" | "settled";
export type WindowResult = "up" | "down";

/** One "BTC Up or Down: 15 min" market window. */
export interface MarketWindow {
  /** stable id; the Polymarket US market slug */
  id: string;
  title: string;
  startTs: number;
  endTs: number;
  /** BTC price to beat (strike), USD; null until known */
  priceToBeat: number | null;
  /** instrument identifiers the backend uses for the Up and Down sides (slug, or slug + side) */
  upInstrument: string;
  downInstrument: string;
  status: WindowStatus;
  result: WindowResult | null;
  /** settlement BTC value if the exchange publishes it */
  settlePrice: number | null;
}

export type TradingMode = "paper" | "live";

export interface Settings {
  mode: TradingMode;
  /** realized-vol lookback, minutes (default 30) */
  volLookbackMin: number;
  /** taker fee, cents per contract; null = unconfirmed -> UI shows [FEE] */
  feeCentsPerContract: number | null;
  /** fee model note shown in settings */
  feeNote: string;
}

export interface StateSnapshot {
  serverTime: number;
  settings: Settings;
  current: MarketWindow | null;
  next: MarketWindow | null;
  /** most recent finished windows, newest first (at least 8 when available) */
  recent: MarketWindow[];
  lastTick: BtcTick | null;
  lastOdds: OddsSnap | null;
  lastFair: FairSnap | null;
  feeds: FeedStatus[];
}

// ---- WebSocket: server -> client. Path: /ws ----
export type ServerMsg =
  | { type: "snapshot"; data: StateSnapshot }
  | { type: "btc"; data: BtcTick }
  | { type: "odds"; data: OddsSnap }
  | { type: "fair"; data: FairSnap }
  | { type: "window"; data: MarketWindow } // any window insert/update (rollover, PTB known, settlement)
  | { type: "rollover"; data: { current: MarketWindow | null; next: MarketWindow | null } }
  | { type: "feeds"; data: FeedStatus[] } // sent every second
  | { type: "settings"; data: Settings };

// ---- WebSocket: client -> server ----
export type ClientMsg = { type: "ping"; ts: number };

// ---- REST (all under /api) ----
// GET  /api/health                         -> { ok: true, serverTime }
// GET  /api/state                          -> StateSnapshot
// GET  /api/history/bars?from&to           -> Bar[]       (ascending; seeded 60s bars + live 1s bars)
// GET  /api/history/odds?from&to           -> OddsSnap[]  (ascending, at most ~1/s)
// GET  /api/history/fair?from&to           -> FairSnap[]  (ascending, at most ~1/s)
// GET  /api/windows?limit=50               -> MarketWindow[] (newest first)
// GET  /api/settings                       -> Settings
// PUT  /api/settings  (partial Settings)   -> Settings
export interface HistoryQuery {
  from: number;
  to: number;
}
