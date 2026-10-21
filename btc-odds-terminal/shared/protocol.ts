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
  /** risk limits (editable; PUT /api/settings {risk: Partial<RiskLimits>} or PUT /api/risk). Absent = DEFAULT_RISK_LIMITS. */
  risk?: RiskLimits;
  /** derived, read-only: POLYMARKET_US_KEY_ID + SECRET are set in the backend .env (live mode is possible) */
  liveAvailable?: boolean;
}

/** PUT /api/settings body: any subset; `risk` merges field by field; feeNote / liveAvailable are ignored. */
export type SettingsPatch = Partial<Omit<Settings, "risk" | "feeNote" | "liveAvailable">> & { risk?: Partial<RiskLimits> };

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
  // ---- steps 3-6 (optional: absent = the module is not running / nothing yet) ----
  /** current window's Up book depth */
  book?: BookLevels | null;
  /** orders of the last 24h, newest first (at most 200) */
  orders?: Order[];
  /** fills of the last 24h, newest first (at most 200) */
  fills?: Fill[];
  /** open positions plus positions closed/settled in the last 24h */
  positions?: Position[];
  /** active brackets (plus ones that changed in the last 24h) */
  brackets?: Bracket[];
  risk?: RiskState;
  alerts?: Alert[];
  /** most recent alert events, newest first (at most 50) */
  alertEvents?: AlertEvent[];
  drawings?: Drawing[];
  live?: LiveStatus;
}

// =====================================================================================================
// Steps 3-6: trading, brackets, risk, drawings, alerts, analysis, replay, live.
// Money: USD (dollars, float). Contract prices: CENTS. Contract quantities: contracts (may be fractional,
// e.g. 96.2; paper rounds to 0.1). "Up"/"Down" are the two outcomes of the current window.
// =====================================================================================================

export type Side = "up" | "down";
export type OrderAction = "buy" | "sell";
/** limit = rests at limitCents (paper: fills when the book crosses); marketable = limit at ask + maxSlippage, IOC */
export type OrderType = "limit" | "marketable";
export type OrderStatus = "pending" | "open" | "partial" | "filled" | "cancelled" | "rejected";
/** why an order exists: user entry/close, a bracket leg, the kill switch, or window settlement */
export type OrderReason = "manual" | "close" | "tp" | "sl" | "btc_stop" | "time_exit" | "kill";

/** Bracket values as entered (ticket / PUT bracket). null = that leg is off. */
export interface BracketInput {
  /** take profit: exit when the position's side BID >= tpCents */
  tpCents: number | null;
  /** stop loss: exit when the position's side BID <= slCents */
  slCents: number | null;
  /** BTC stop (USD): Up position exits when BTC < btcStop; Down position exits when BTC > btcStop */
  btcStop: number | null;
  /** time exit: exit when the window has <= timeExitSec seconds left */
  timeExitSec: number | null;
  /** slippage cap for the exit order, cents (default: the entry order's maxSlippageCents) */
  maxSlippageCents?: number | null;
}

export type BracketStatus = "active" | "triggered" | "cancelled" | "done";

/** OCO bracket attached to one position; enforced by the backend watcher (no native bracket orders exist). */
export interface Bracket extends BracketInput {
  id: string;
  positionId: string;
  windowId: string;
  maxSlippageCents: number | null;
  status: BracketStatus;
  triggerReason: OrderReason | null;
  triggeredTs: number | null;
  /** the exit order placed when it triggered */
  exitOrderId: string | null;
  createdTs: number;
  updatedTs: number;
}

export interface Order {
  id: string;
  clientOrderId: string | null;
  /** exchange order id (live only) */
  externalId: string | null;
  mode: TradingMode;
  windowId: string;
  /** MarketWindow.upInstrument / downInstrument */
  instrument: string;
  side: Side;
  action: OrderAction;
  type: OrderType;
  /** limit price, cents (marketable: ask + maxSlippage at submit, buy; bid - maxSlippage, sell) */
  limitCents: number | null;
  maxSlippageCents: number | null;
  /** contracts requested */
  qty: number;
  /** USD amount requested (buys from the ticket) */
  notionalUsd: number | null;
  filledQty: number;
  avgFillCents: number | null;
  feesUsd: number;
  status: OrderStatus;
  reason: OrderReason;
  bracketId: string | null;
  positionId: string | null;
  /** reject / error text */
  error: string | null;
  createdTs: number;
  updatedTs: number;
}

export interface Fill {
  id: string;
  orderId: string;
  externalId: string | null;
  mode: TradingMode;
  windowId: string;
  instrument: string;
  side: Side;
  action: OrderAction;
  priceCents: number;
  qty: number;
  /** USD; null = fee unknown ([FEE]: unconfirmed schedule and no override) */
  feeUsd: number | null;
  /** cents vs the reference price at submit (best ask for buys, best bid for sells); positive = worse */
  slippageCents: number | null;
  ts: number;
}

export type PositionStatus = "open" | "closed" | "settled";

export interface Position {
  id: string;
  mode: TradingMode;
  windowId: string;
  instrument: string;
  side: Side;
  /** open contracts (0 once closed) */
  qty: number;
  /** average entry, cents */
  avgCents: number;
  /** USD paid for the open contracts (qty * avgCents / 100), fees excluded */
  costUsd: number;
  realizedPnlUsd: number;
  feesUsd: number;
  status: PositionStatus;
  /** mark = this side's best bid, cents (null when no quote); settled: 100 or 0 */
  markCents: number | null;
  /** qty * (mark - avg) / 100 - unpaid exit fees ignored; null when no mark */
  unrealizedPnlUsd: number | null;
  bracketId: string | null;
  openedTs: number;
  updatedTs: number;
  closedTs: number | null;
}

/** POST /api/orders body. */
export interface PlaceOrderRequest {
  side: Side;
  type: OrderType;
  /** USD to spend (buy). Contracts = usd / fill price. */
  usd: number;
  /** required for type "limit" */
  limitCents?: number | null;
  /** marketable cap and default exit slippage, cents */
  maxSlippageCents: number;
  bracket?: BracketInput | null;
  /** live mode only: the session id from POST /api/live/confirm */
  liveSession?: string | null;
  clientOrderId?: string | null;
}

export interface PlaceOrderResponse {
  order: Order;
  fills: Fill[];
  position: Position | null;
  bracket: Bracket | null;
}

/** POST /api/positions/:id/close body (optional). */
export interface ClosePositionRequest {
  maxSlippageCents?: number | null;
  liveSession?: string | null;
}

export interface RiskLimits {
  /** realized + unrealized loss today (ET day) at which trading auto-disables until the next ET day, USD */
  dailyLossLimitUsd: number;
  /** max total cost of open positions + resting buy orders, USD */
  maxExposureUsd: number;
  /** max loss (cost at risk) in one window, USD */
  maxLossPerWindowUsd: number;
  /** max cost of one position, USD */
  maxPositionUsd: number;
}

export const DEFAULT_RISK_LIMITS: RiskLimits = {
  dailyLossLimitUsd: 200,
  maxExposureUsd: 500,
  maxLossPerWindowUsd: 100,
  maxPositionUsd: 250,
};

export interface RiskState {
  limits: RiskLimits;
  mode: TradingMode;
  /** America/New_York calendar day the daily counters belong to, "YYYY-MM-DD" */
  day: string;
  /** realized P&L today (ET), USD (fees included) */
  dailyRealizedPnlUsd: number;
  /** loss counted against dailyLossLimitUsd (>= 0), USD */
  dailyLossUsedUsd: number;
  /** cost of open positions + resting buys, USD */
  openExposureUsd: number;
  /** current window: cost at risk (open cost + realized loss in the window), USD */
  windowLossUsedUsd: number;
  /** trading is disabled until this ts (next ET midnight after the daily limit hit); null = enabled */
  tradingDisabledUntil: number | null;
  disabledReason: string | null;
  /** the kill switch ran and trading stays off until re-armed (PUT /api/risk {killSwitch:false}) */
  killSwitch: boolean;
  updatedTs: number;
}

/**
 * PUT /api/risk body. killSwitch:false re-arms after the kill switch. clearDisable:true lifts today's daily-loss
 * disable (refused with 400 while today's loss is still at the limit: raise dailyLossLimitUsd in the same PUT).
 */
export type RiskPatch = Partial<RiskLimits> & { killSwitch?: false; clearDisable?: true };

export interface KillResult {
  cancelledOrders: number;
  closedPositions: number;
  risk: RiskState;
}

// ---- order book ----

export interface BookLevel {
  /** cents */
  price: number;
  /** contracts */
  size: number;
}

/** Up-side book depth of the current window (GET /v1/markets/{slug}/book, flipped when Up is the short side). */
export interface BookLevels {
  ts: number;
  windowId: string;
  instrument: string;
  /** best (highest) first */
  bids: BookLevel[];
  /** best (lowest) first */
  asks: BookLevel[];
  /** MARKET_STATE_* or null */
  state: string | null;
}

// ---- drawings ----

export type DrawingKind = "trend" | "ray" | "hline" | "vline" | "rect" | "fib" | "text" | "measure" | "sticker";
export type StickerKind = "target" | "rocket" | "warning" | "check" | "cross" | "bull" | "bear" | "lightning";
export type DrawingScale = "btc" | "odds";

export interface DrawingPoint {
  /** epoch ms */
  time: number;
  /** BTC USD (scale "btc") or cents (scale "odds") */
  price: number;
  scale: DrawingScale;
}

/** Fields a client sends on create (POST) / update (PUT, partial). */
export interface DrawingInput {
  kind: DrawingKind;
  /** trend/ray/rect/fib/measure: 2 points; hline/vline/text/sticker: 1 point */
  points: DrawingPoint[];
  /** CSS hex color */
  color: string;
  locked: boolean;
  /** inline label (hline) / note text (text) */
  label: string | null;
  sticker?: StickerKind | null;
  lineWidth?: number;
  dashed?: boolean;
}

export interface Drawing extends DrawingInput {
  id: string;
  createdTs: number;
  updatedTs: number;
}

// ---- alerts ----

export type AlertKind =
  | "btc_above"
  | "btc_below"
  | "up_above"
  | "up_below"
  | "down_above"
  | "down_below"
  | "edge_above"
  | "edge_below"
  | "btc_cross_ptb";

/** POST /api/alerts body; PUT takes a partial. */
export interface AlertInput {
  kind: AlertKind;
  /** btc_*: USD; up_ / down_: cents (mid); edge_: cents; btc_cross_ptb: ignored (null) */
  threshold: number | null;
  enabled: boolean;
  /** disable after the first fire */
  once: boolean;
  note: string | null;
}

export interface Alert extends AlertInput {
  id: string;
  lastFiredTs: number | null;
  createdTs: number;
  updatedTs: number;
}

export interface AlertEvent {
  id: number;
  alertId: string;
  kind: AlertKind;
  ts: number;
  windowId: string | null;
  /** the observed value that fired it (USD / cents) */
  value: number | null;
  /** human sentence, e.g. "BTC crossed above price to beat ($67,012.50)" */
  message: string;
}

// ---- analysis (GET /api/analysis?scope=) ----

export type AnalysisScope = "window" | "last20" | "all";

/** One sample: BTC distance from PTB in sigma units vs Up mid. */
export interface ScatterPoint {
  ts: number;
  windowId: string;
  /** ln(S/K) / (sigma * sqrt(tLeft)) */
  z: number;
  upMid: number;
  tLeftSec: number;
}

export interface LagBar {
  /** seconds (0, 2, 4 ... 30) */
  lagSec: number;
  /** cross-correlation of BTC returns with odds changes lagSec later; null = not enough data */
  rho: number | null;
  n: number;
}

/** Cents of Up per $10 of BTC at time ts. */
export interface SensitivityPoint {
  ts: number;
  tLeftSec: number;
  market: number | null;
  fair: number | null;
}

export interface AnalysisPayload {
  scope: AnalysisScope;
  computedTs: number;
  /** window ids included */
  windowIds: string[];
  scatter: ScatterPoint[];
  /** fair P(Up) in cents vs z for the current time left */
  fairCurve: { z: number; fair: number }[];
  current: { z: number; upMid: number; fair: number | null; tLeftSec: number } | null;
  lag: LagBar[];
  lagPeak: { lagSec: number; rho: number } | null;
  /** current window only (scope ignored) */
  sensitivity: SensitivityPoint[];
  /** one-line takeaways generated from the numbers */
  takeaways: { scatter: string; lag: string; sensitivity: string };
}

// ---- replay (GET /api/replay/:windowId) ----

/** One replay step (~1s). */
export interface ReplayFrame {
  ts: number;
  /** BRTI proxy close for the second */
  btc: number | null;
  upBid: number | null;
  upAsk: number | null;
  upMid: number | null;
  fair: number | null;
  edge: number | null;
}

export interface ReplayData {
  window: MarketWindow;
  /** 1s bars covering [window.startTs - leadMs, window.endTs] */
  bars: Bar[];
  odds: OddsSnap[];
  fair: FairSnap[];
  frames: ReplayFrame[];
  /** my fills in that window (markers) */
  fills: Fill[];
  /** BRTI-proxy ticks of the window (thinned to ~4/s); [] once older than tick retention (bars only then) */
  ticks?: BtcTick[];
}

// ---- live trading ----

export interface LiveStatus {
  /** keys present in the backend .env */
  available: boolean;
  /** settings.mode === "live" */
  enabled: boolean;
  /** private WebSocket connected */
  connected: boolean;
  /** last reconcile with GET /v1/portfolio/positions */
  lastReconcileTs: number | null;
  balanceUsd: number | null;
  buyingPowerUsd: number | null;
  error: string | null;
}

/** POST /api/live/confirm -> the session id to send as PlaceOrderRequest.liveSession (valid until expiresTs). */
export interface LiveConfirmResponse {
  session: string;
  expiresTs: number;
}

/** Server-side notice for a toast (bracket triggered, order rejected, trading disabled...). */
export interface Notice {
  id: string;
  ts: number;
  level: "info" | "success" | "warn" | "error";
  title: string;
  message: string;
}

/** Error body of every non-2xx REST response. `code` is set for errors a client branches on. */
export interface ApiErrorBody {
  error: string;
  code?: ApiErrorCode;
}

export type ApiErrorCode =
  | "validation" // 400
  | "not_found" // 404
  | "risk_rejected" // 403: a risk limit would be exceeded
  | "trading_disabled" // 423: daily loss limit hit or kill switch engaged
  | "live_confirm_required" // 409: live mode and no confirmed liveSession
  | "live_unavailable" // 409: no keys / not in live mode
  | "no_market" // 409: no live window / no quote
  | "exchange_error"; // 502: Polymarket US rejected or failed

// ---- WebSocket: server -> client. Path: /ws ----
export type ServerMsg =
  | { type: "snapshot"; data: StateSnapshot }
  | { type: "btc"; data: BtcTick }
  | { type: "odds"; data: OddsSnap }
  | { type: "fair"; data: FairSnap }
  | { type: "window"; data: MarketWindow } // any window insert/update (rollover, PTB known, settlement)
  | { type: "rollover"; data: { current: MarketWindow | null; next: MarketWindow | null } }
  | { type: "feeds"; data: FeedStatus[] } // sent every second
  | { type: "settings"; data: Settings }
  // ---- steps 3-6 ----
  | { type: "book"; data: BookLevels } // current window's Up book, ~1/s (droppable)
  | { type: "orders"; data: Order[] } // upserts by id
  | { type: "fills"; data: Fill[] } // new fills (dedupe by id)
  | { type: "positions"; data: Position[] } // upserts by id (status tells open / closed / settled)
  | { type: "brackets"; data: Bracket[] } // upserts by id
  | { type: "risk"; data: RiskState } // replace
  | { type: "alerts"; data: Alert[] } // full list, replace
  | { type: "alertFired"; data: AlertEvent }
  | { type: "drawings"; data: Drawing[] } // full list, replace
  | { type: "live"; data: LiveStatus }
  | { type: "notice"; data: Notice };

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
// PUT  /api/settings  SettingsPatch        -> Settings   (risk: Partial<RiskLimits> merges; liveAvailable ignored)
//
// ---- steps 3-6 (errors: ApiErrorBody with `code`) ----
// GET    /api/book                          -> BookLevels | null
// POST   /api/orders  PlaceOrderRequest     -> PlaceOrderResponse   (403 risk_rejected, 423 trading_disabled,
//                                                                     409 live_confirm_required / no_market)
// GET    /api/orders?limit=200&windowId&status -> Order[]      (newest first)
// DELETE /api/orders/:id                    -> Order        (cancel a resting order)
// GET    /api/fills?limit=200&windowId      -> Fill[]       (newest first)
// GET    /api/positions?status=open|all&limit=200 -> Position[] (newest first)
// POST   /api/positions/:id/close  ClosePositionRequest? -> PlaceOrderResponse
// PUT    /api/positions/:id/bracket  BracketInput | null  -> Bracket | null   (null / all legs null = remove)
// GET    /api/brackets?status=active        -> Bracket[]
// POST   /api/kill                          -> KillResult   (cancel all + flatten; sets risk.killSwitch)
// GET    /api/risk                          -> RiskState
// PUT    /api/risk  RiskPatch               -> RiskState    (limits persist in Settings.risk)
// GET    /api/drawings                      -> Drawing[]
// POST   /api/drawings  DrawingInput        -> Drawing
// PUT    /api/drawings  Drawing[]           -> Drawing[]    (replace all; undo of "delete all")
// PUT    /api/drawings/:id  Partial<DrawingInput> -> Drawing
// DELETE /api/drawings/:id                  -> { ok: true }
// DELETE /api/drawings                      -> { deleted: number }
// GET    /api/alerts                        -> Alert[]
// POST   /api/alerts  AlertInput            -> Alert
// PUT    /api/alerts/:id  Partial<AlertInput> -> Alert
// DELETE /api/alerts/:id                    -> { ok: true }
// GET    /api/alerts/events?limit=50        -> AlertEvent[] (newest first)
// GET    /api/analysis?scope=window|last20|all -> AnalysisPayload
// GET    /api/replay/:windowId              -> ReplayData   (404 not_found)
// GET    /api/live/status                   -> LiveStatus
// POST   /api/live/confirm                  -> LiveConfirmResponse (409 live_unavailable unless mode = live + keys)
// POST   /api/live/reconcile                -> LiveStatus & { deferred: boolean } (reconcile with the exchange now)
export interface HistoryQuery {
  from: number;
  to: number;
}
