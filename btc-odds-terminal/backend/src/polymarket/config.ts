// Environment-driven settings for the Polymarket US market-data module.
// Everything that docs/POLYMARKET_US.md marks UNVERIFIED is overridable here so a wrong guess
// can be fixed from .env without a code change.

export type WsMode = "auto" | "off" | "noauth";

export interface PolyConfig {
  /** public market-data host (verified: official SDK GATEWAY_BASE_URL) */
  gatewayUrl: string;
  /** markets WebSocket URL (verified: SDK builds wss://api.polymarket.us + /v1/ws/markets) */
  wsUrl: string;
  /** auto = WS when API keys are present (the WS requires Ed25519 auth), else poll; off = always poll;
   *  noauth = try the WS without auth headers (falls back to polling while it fails) */
  wsMode: WsMode;
  keyId: string | null;
  secretKey: string | null;
  /** request timeout, ms */
  timeoutMs: number;
  /** minimum gap between public-gateway requests, all callers combined (rate limit seen live: 429/1015) */
  minGapMs: number;
  /** discovery: free-text query for GET /v1/search (UNVERIFIED that this finds the series) */
  searchQuery: string;
  /** discovery: optional series id(s) for GET /v1/events?seriesId= (UNVERIFIED id) */
  seriesIds: number[];
  /** discovery: optional tag slug for GET /v1/events?tagSlug= */
  tagSlug: string | null;
  /** discovery: optional regex an event/market slug must match (overrides the title heuristics) */
  slugRegex: RegExp | null;
  /** metadata keys searched (market first, then event) for the price to beat (UNVERIFIED names) */
  ptbFields: string[];
  /** discovery: event slug of a window, from its UTC start. Tokens {YYYY} {MM} {DD} {HH} {mm}.
   *  Seen live 2026-10-21: "btc-updown-15m-2026-10-21-1415z". Set POLY_EVENT_SLUG_TEMPLATE=off to disable. */
  eventSlugTemplate: string | null;
  /** window length, ms (15 minutes) */
  windowMs: number;
}

export const DEFAULT_EVENT_SLUG_TEMPLATE = "btc-updown-15m-{YYYY}-{MM}-{DD}-{HH}{mm}z";

function str(v: string | undefined): string | null {
  return v !== undefined && v.trim() !== "" ? v.trim() : null;
}

export const DEFAULT_PTB_FIELDS = [
  "priceToBeat",
  "price_to_beat",
  "strikePrice",
  "strike",
  "threshold",
  "line",
  "referencePrice",
  "openPrice",
  "startPrice",
  "targetPrice",
];

export function loadPolyConfig(env: NodeJS.ProcessEnv = process.env): PolyConfig {
  const wsModeRaw = (str(env.POLY_WS_MODE) ?? "auto").toLowerCase();
  const wsMode: WsMode = wsModeRaw === "off" || wsModeRaw === "noauth" ? wsModeRaw : "auto";
  const slugRe = str(env.POLY_SLUG_REGEX);
  let slugRegex: RegExp | null = null;
  if (slugRe) {
    try {
      slugRegex = new RegExp(slugRe, "i");
    } catch {
      slugRegex = null;
    }
  }
  const timeout = Number(env.POLY_TIMEOUT_MS);
  return {
    gatewayUrl: (str(env.POLY_GATEWAY_URL) ?? "https://gateway.polymarket.us").replace(/\/+$/, ""),
    wsUrl: str(env.POLY_WS_URL) ?? "wss://api.polymarket.us/v1/ws/markets",
    wsMode,
    keyId: str(env.POLYMARKET_US_KEY_ID),
    secretKey: str(env.POLYMARKET_US_SECRET),
    timeoutMs: Number.isFinite(timeout) && timeout > 0 ? timeout : 5000,
    minGapMs: (() => {
      const g = Number(env.POLY_MIN_GAP_MS);
      return Number.isFinite(g) && g >= 0 ? g : 1000;
    })(),
    searchQuery: str(env.POLY_SEARCH_QUERY) ?? "Bitcoin Up or Down",
    seriesIds: (str(env.POLY_SERIES_ID) ?? "")
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isInteger(n) && n > 0),
    tagSlug: str(env.POLY_TAG_SLUG),
    slugRegex,
    ptbFields: (str(env.POLY_PTB_FIELDS) ?? DEFAULT_PTB_FIELDS.join(","))
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    eventSlugTemplate: (() => {
      const t = str(env.POLY_EVENT_SLUG_TEMPLATE);
      if (t === null) return DEFAULT_EVENT_SLUG_TEMPLATE;
      return t.toLowerCase() === "off" ? null : t;
    })(),
    windowMs: 15 * 60 * 1000,
  };
}
