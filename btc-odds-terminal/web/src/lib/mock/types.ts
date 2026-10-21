// Contract for the web mock's feature modules (mock mode = NEXT_PUBLIC_MOCK=1 or ?mock=1). OWNER: scaffold.
//
// lib/mock.ts (the market simulator) builds every module in mock/index.ts with one MockCtx, calls their hooks
// from its 250ms step loop, merges their snapshot() into StateSnapshot, and lib/mock/router.ts dispatches
// every REST call made through api.ts to the module `routes`. Modules mirror the backend semantics so the whole
// UI is demoable without a backend. Throw ApiError (lib/api-error.ts) for error responses.

import type {
  Alert,
  AlertEvent,
  BookLevels,
  BtcTick,
  Drawing,
  FairSnap,
  Bar,
  Fill,
  KillResult,
  LiveStatus,
  MarketWindow,
  OddsSnap,
  OrderReason,
  Position,
  RiskState,
  ServerMsg,
  Settings,
  SettingsPatch,
  StateSnapshot,
  TradingMode,
} from "@shared/protocol";
import type { RiskCheckResult, RiskOrderCheck } from "@shared/risk";

export type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

export interface MockRequest {
  params: Record<string, string>;
  query: Record<string, string>;
  body: unknown;
}

export interface MockRoute {
  method: HttpMethod;
  /** path under /api, with :params, e.g. "/orders/:id" */
  path: string;
  handler: (req: MockRequest) => unknown | Promise<unknown>;
}

/** What the simulator exposes to modules. All prices in cents / USD like the wire protocol. */
export interface MockCtx {
  emit(m: ServerMsg): void;
  /** simulated clock (ms); equals wall clock once history is built */
  now(): number;
  current(): MarketWindow;
  next(): MarketWindow;
  getWindow(id: string): MarketWindow | null;
  /** newest first */
  windows(limit: number): MarketWindow[];
  lastTick(): BtcTick | null;
  lastOdds(): OddsSnap | null;
  lastFair(): FairSnap | null;
  /** current window's simulated Up book */
  book(): BookLevels | null;
  settings(): Settings;
  /** apply a settings patch (emits `settings`, calls every module's onSettings) */
  putSettings(p: SettingsPatch): Settings;
  bars(from: number, to: number): Bar[];
  odds(from: number, to: number): OddsSnap[];
  fair(from: number, to: number): FairSnap[];
  /** BRTI-proxy ticks (seeded history ~1/s, live steps 4/s), ascending */
  ticks(from: number, to: number): BtcTick[];
  /** BRTI-proxy price at ms (1s resolution) */
  priceAt(ms: number): number;
  /** late-bound (filled after every module exists): use only inside hooks / handlers */
  modules: MockModules;
}

export interface MockModule {
  name: string;
  routes?: MockRoute[];
  /** every 250ms simulated step, after the btc message */
  onStep?(ts: number): void;
  /** every simulated second, after odds / fair / book messages */
  onSecond?(ts: number): void;
  /** window boundary: `settled` just ended (result known), `current` just went live */
  onRollover?(settled: MarketWindow, current: MarketWindow): void;
  /** settings changed (mode / risk / fee) */
  onSettings?(s: Settings): void;
  /** merged into every StateSnapshot */
  snapshot?(): Partial<StateSnapshot>;
}

/** mock/trading.ts (trading-backend) */
export interface MockTrading extends MockModule {
  positions(): Position[];
  fills(windowId?: string): Fill[];
  riskInputs(windowId: string | null): {
    dailyRealizedPnlUsd: number;
    unrealizedPnlUsd: number;
    openExposureUsd: number;
    windowLossUsedUsd: number;
  };
  cancelAll(reason: OrderReason): number;
  /** flatten every open position; returns how many ended flat (the rest retry via a triggered `kill` bracket) */
  flattenAll(reason: OrderReason): number;
  /** daily-loss disable: cancel resting (pending / open / partial) buys of that mode; returns the count */
  cancelRestingBuys?(mode: TradingMode): number;
}

/** mock/risk.ts (alerts-risk) */
export interface MockRisk extends MockModule {
  state(): RiskState;
  check(o: RiskOrderCheck): RiskCheckResult;
  refresh(): RiskState;
  kill(): KillResult;
}

/** mock/live.ts (live-backend) */
export interface MockLive extends MockModule {
  status(): LiveStatus;
  isSessionConfirmed(session: string | null | undefined): boolean;
}

/** mock/alerts.ts (alerts-risk) */
export interface MockAlerts extends MockModule {
  list(): Alert[];
  events(limit?: number): AlertEvent[];
}

/** mock/drawings.ts (drawings) */
export interface MockDrawings extends MockModule {
  list(): Drawing[];
}

export interface MockModules {
  trading: MockTrading;
  risk: MockRisk;
  live: MockLive;
  alerts: MockAlerts;
  drawings: MockDrawings;
  analysis: MockModule;
  replay: MockModule;
}
