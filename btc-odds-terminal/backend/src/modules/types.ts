// Contract between the backend modules of steps 3-6 (trading, risk, live, alerts, drawings, analysis, replay).
// OWNER: scaffold (do not edit in a section; ask for a change instead).
//
// Every module folder exports `create<Name>(deps: ModuleDeps): BackendModule<Service>`; modules/index.ts builds
// them all with ONE shared deps object and fills `deps.services` once every module exists. So:
//   - never touch deps.services.* inside create<Name>() itself; only in start(), route handlers and listeners;
//   - SQL lives in your own folder (prepared statements on deps.db.raw); do not edit db/index.ts or
//     db/migrations.ts (v2 already has orders / fills / positions / brackets / risk_daily / drawings / alerts /
//     alert_events). If you need more, CREATE TABLE IF NOT EXISTS <module>_* in your create<Name>().

import type { FastifyInstance } from "fastify";
import type {
  AlertEvent,
  Alert,
  AnalysisPayload,
  AnalysisScope,
  BookLevels,
  Bracket,
  BracketInput,
  BtcTick,
  ClosePositionRequest,
  Drawing,
  FairSnap,
  Fill,
  KillResult,
  LiveStatus,
  MarketWindow,
  OddsSnap,
  Order,
  OrderReason,
  OrderStatus,
  PlaceOrderRequest,
  PlaceOrderResponse,
  Position,
  ReplayData,
  RiskState,
  ServerMsg,
  Settings,
  StateSnapshot,
  TradingMode,
} from "../../../shared/protocol.ts";
import type { RiskCheckResult, RiskOrderCheck } from "../../../shared/risk.ts";
import type { Config } from "../config.ts";
import type { TerminalDb } from "../db/index.ts";
import type { EngineEvents } from "../engine.ts";
import type { Logger } from "../log.ts";

export type { EngineEvents } from "../engine.ts";

/** What modules may use from the Engine (Engine implements it). */
export interface EngineApi {
  on<K extends keyof EngineEvents>(event: K, listener: (arg: EngineEvents[K]) => void): unknown;
  off<K extends keyof EngineEvents>(event: K, listener: (arg: EngineEvents[K]) => void): unknown;
  snapshot(): StateSnapshot;
  getSettings(): Settings;
  /** partial Settings (incl. { risk: Partial<RiskLimits> }); throws statusCode 400 on invalid input */
  updateSettings(patch: unknown): Settings;
  currentWindow(): MarketWindow | null;
  nextWindow(): MarketWindow | null;
  latestTick(): BtcTick | null;
  /** current window's top of book, null when stale (> 3s) or another window's */
  latestOdds(now?: number): OddsSnap | null;
  latestFair(now?: number): FairSnap | null;
  /** cached Up book of the current window (check .ts) */
  currentBook(): BookLevels | null;
  /** fetch the Up book now (falls back to the cache) */
  refreshBook(): Promise<BookLevels | null>;
  /** BRTI-proxy price at ts (1s bars), null if unknown */
  priceAt(ts: number, tolMs?: number): number | null;
  /** keys present in .env */
  liveAvailable(): boolean;
  /** refuse a settings mode change (return a message -> 400); optional so test fakes need not implement it */
  addSettingsGuard?(fn: (next: { mode: TradingMode }, prev: Settings) => string | null): void;
}

/** The ONE deps object every module receives. */
export interface ModuleDeps {
  config: Config;
  db: TerminalDb;
  engine: EngineApi;
  /** send to every WebSocket client */
  broadcast(msg: ServerMsg): void;
  logger: Logger;
  /** test seam for clocks */
  now(): number;
  /** late-bound: filled after every module was created */
  services: Services;
}

export interface BackendModule<S> {
  name: string;
  service: S;
  /** add REST routes (all under /api) */
  register(app: FastifyInstance): void;
  /** subscribe to engine events / start timers (called after engine.start()) */
  start?(): void;
  stop?(): void | Promise<void>;
  /** fields merged into every StateSnapshot (e.g. { orders, positions }) */
  snapshot?(): Partial<StateSnapshot>;
}

// ---------------------------------------------------------------- execution (trading <-> live)

/** One execution reported by an executor (paper simulation or the exchange). */
export interface ExecFill {
  externalId: string | null;
  priceCents: number;
  qty: number;
  /** USD; null = unknown ([FEE]) */
  feeUsd: number | null;
  ts: number;
}

export interface ExecResult {
  externalId: string | null;
  status: OrderStatus;
  fills: ExecFill[];
  error?: string | null;
}

export interface ExecContext {
  window: MarketWindow;
  /** Up book (fresh from engine.refreshBook()); Down orders use shared/paper.ts sideBook() */
  book: BookLevels | null;
  odds: OddsSnap | null;
  feeCentsPerContract: number | null;
}

/** Places orders somewhere: PaperExecutor (trading-backend) or LiveExecutor (live-backend). */
export interface Executor {
  readonly mode: TradingMode;
  submit(order: Order, ctx: ExecContext): Promise<ExecResult>;
  cancel(order: Order): Promise<void>;
  /** live only: the exchange's cancel-all for these market slugs (the kill switch's backstop); returns order ids */
  cancelAllRemote?(slugs?: string[]): Promise<string[]>;
}

/** Async order/fill update from the private WebSocket (live). */
export interface ExternalOrderUpdate {
  externalId: string;
  status?: OrderStatus;
  fill?: ExecFill;
  error?: string | null;
}

export interface RemotePosition {
  /** MarketWindow.upInstrument / downInstrument form ("<slug>:long" | "<slug>:short") */
  instrument: string;
  qty: number;
  avgCents: number | null;
}

// ---------------------------------------------------------------- services

export interface OrderQuery {
  limit?: number;
  windowId?: string;
  status?: OrderStatus;
}

/** trading-backend (backend/src/trading). */
export interface TradingService {
  placeOrder(req: PlaceOrderRequest, opts?: { reason?: OrderReason }): Promise<PlaceOrderResponse>;
  cancelOrder(id: string): Promise<Order>;
  /** cancel every resting order; returns the count */
  cancelAll(reason: OrderReason): Promise<number>;
  closePosition(id: string, req?: ClosePositionRequest & { reason?: OrderReason }): Promise<PlaceOrderResponse>;
  /** close every open position with marketable orders; returns the count */
  flattenAll(reason: OrderReason): Promise<number>;
  /** null / all legs null = remove the bracket */
  setBracket(positionId: string, b: BracketInput | null): Bracket | null;
  orders(q?: OrderQuery): Order[];
  fills(q?: { limit?: number; windowId?: string }): Fill[];
  positions(q?: { status?: "open" | "all"; limit?: number }): Position[];
  brackets(q?: { status?: "active" | "all" }): Bracket[];
  /** numbers risk needs (USD) for the current mode; windowId = current window */
  riskInputs(windowId: string | null): {
    dailyRealizedPnlUsd: number;
    unrealizedPnlUsd: number;
    openExposureUsd: number;
    windowLossUsedUsd: number;
  };
  /** live: apply a private-WS order/fill update */
  applyExternal(u: ExternalOrderUpdate): void;
  /** live: reconcile local open positions with GET /v1/portfolio/positions */
  reconcile(remote: RemotePosition[]): void;
}

/** alerts-risk (backend/src/risk). */
export interface RiskService {
  state(): RiskState;
  /** pre-trade check; trading calls it for every non-reducing order */
  check(o: RiskOrderCheck): RiskCheckResult;
  /** recompute (after fills / marks / limit changes); broadcasts `risk` when it changed */
  refresh(): RiskState;
  /** cancel all + flatten + engage the kill switch */
  kill(): Promise<KillResult>;
}

/** live-backend (backend/src/live). */
export interface LiveService {
  status(): LiveStatus;
  /** session id from POST /api/live/confirm and not expired */
  isSessionConfirmed(session: string | null | undefined): boolean;
  /**
   * exchange executor; null unless settings.mode === "live" and keys are set. Trading refuses a live -> paper
   * switch while a live position of a still-trading window is open (engine settings guard), so live exits never
   * need the executor in paper mode; resting live orders are drained by the live module on that switch.
   */
  executor(): Executor | null;
}

/** alerts-risk (backend/src/alerts). */
export interface AlertsService {
  list(): Alert[];
  events(limit?: number): AlertEvent[];
}

/** drawings (backend/src/drawings). */
export interface DrawingsService {
  list(): Drawing[];
}

/** analysis (backend/src/analysis). */
export interface AnalysisService {
  compute(scope: AnalysisScope): AnalysisPayload;
}

/** replay (backend/src/replay). */
export interface ReplayService {
  load(windowId: string): ReplayData | null;
}

export interface Services {
  trading: TradingService;
  risk: RiskService;
  live: LiveService;
  alerts: AlertsService;
  drawings: DrawingsService;
  analysis: AnalysisService;
  replay: ReplayService;
}
