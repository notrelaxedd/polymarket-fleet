export { MarketTracker, buildOddsSnap, type TrackerOptions } from "./tracker.ts";
export { PolymarketClient, PolyHttpError, type MarketDataSource, type Query, type RawEvent, type RawMarket } from "./client.ts";
export {
  windowsFromEvents,
  pickCurrentNext,
  mergeWindow,
  statusAt,
  extractTimes,
  extractPriceToBeat,
  isBtc15mEvent,
  instrumentOf,
  parseInstrument,
  sidePrices,
  type DiscoveredWindow,
  type DiscoveryConfig,
  type Side,
} from "./discovery.ts";
export { FEE_SCHEDULE, feeCents, feeCentsFromRate } from "./fees.ts";
export {
  parseBbo,
  parseBook,
  parseSettlement,
  settlementToResult,
  parseWsMessage,
  toCents,
  type NormalizedBbo,
  type NormalizedBook,
  type NormalizedSettlement,
} from "./normalize.ts";
export { loadPolyConfig, type PolyConfig } from "./config.ts";
export { createAuthHeaders } from "./auth.ts";
export { MarketsStream, backoffMs } from "./ws.ts";
