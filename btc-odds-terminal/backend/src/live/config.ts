// Live-trading settings. Keys come from the backend Config (POLYMARKET_US_KEY_ID / _SECRET); everything that
// docs/POLYMARKET_US.md marks UNVERIFIED about order placement is overridable from .env so a wrong guess can be
// fixed without a code change. Nothing here is ever sent to the frontend.

import type { Config } from "../config.ts";
import { loadPolyConfig } from "../polymarket/config.ts";

/**
 * Which instrument a SHORT-intent price quotes (UNVERIFIED):
 *  - "short": the price is the short instrument's own price (BUY_SHORT @ 0.40 buys Down at 40c). The third-party Go
 *    client states this ("prices always quote the named instrument — there is no 100-p inversion").
 *  - "long": the price is always the long instrument's (BUY_SHORT @ 0.60 buys Down at 40c).
 */
export type ShortPriceQuote = "short" | "long";

export interface LiveConfig {
  keyId: string | null;
  secretKey: string | null;
  /** trading REST host (VERIFIED: SDK API_BASE_URL) */
  apiUrl: string;
  /** private WebSocket (VERIFIED: SDK builds wss://api.polymarket.us + /v1/ws/private) */
  privateWsUrl: string;
  /** REST timeout, ms */
  timeoutMs: number;
  /** periodic GET /v1/portfolio/positions + /v1/account/balances while live, ms */
  reconcileMs: number;
  shortPriceQuote: ShortPriceQuote;
  /** order quantity step, contracts (UNVERIFIED: the SDK types `quantity: number`; whole contracts by default) */
  qtyStep: number;
  /** price tick, cents (UNVERIFIED; 1c by default). Buys round down, sells round up: never worse than asked. */
  tickCents: number;
}

function str(v: string | undefined): string | null {
  return v !== undefined && v.trim() !== "" ? v.trim() : null;
}

function pos(v: string | undefined, dflt: number): number {
  const n = Number(v);
  return v !== undefined && v.trim() !== "" && Number.isFinite(n) && n > 0 ? n : dflt;
}

export function loadLiveConfig(config: Pick<Config, "polymarketUsKeyId" | "polymarketUsSecret">, env: NodeJS.ProcessEnv = process.env): LiveConfig {
  const poly = loadPolyConfig(env);
  const apiUrl = (str(env.POLY_API_URL) ?? "https://api.polymarket.us").replace(/\/+$/, "");
  const wsBase = apiUrl.replace(/^http/, "ws");
  const quote = (str(env.POLY_SHORT_PRICE_QUOTE) ?? "short").toLowerCase();
  return {
    keyId: config.polymarketUsKeyId,
    secretKey: config.polymarketUsSecret,
    apiUrl,
    privateWsUrl: str(env.POLY_PRIVATE_WS_URL) ?? `${wsBase}/v1/ws/private`,
    timeoutMs: poly.timeoutMs,
    reconcileMs: Math.max(2000, pos(env.LIVE_RECONCILE_MS, 15_000)),
    shortPriceQuote: quote === "long" ? "long" : "short",
    qtyStep: pos(env.POLY_QTY_STEP, 1),
    tickCents: pos(env.POLY_TICK_CENTS, 1),
  };
}
