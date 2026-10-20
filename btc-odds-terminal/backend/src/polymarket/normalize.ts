// Pure, defensive parsers for Polymarket US gateway / WebSocket payloads.
// Wire shapes follow the official SDK types (github.com/Polymarket/polymarket-us-typescript,
// src/types/markets.ts and src/websocket/types.ts); see docs/POLYMARKET_US.md.
// Prices on the wire are USD decimal strings inside an Amount ({ value: "0.55", currency: "USD" });
// everything returned from here is in CENTS.

export interface NormalizedBbo {
  slug: string;
  /** best bid of the LONG side, cents (null = empty side) */
  bid: number | null;
  /** best ask of the LONG side, cents */
  ask: number | null;
  last: number | null;
  bidDepth: number | null;
  askDepth: number | null;
}

export interface BookLevel {
  price: number; // cents
  qty: number; // contracts
}

export interface NormalizedBook {
  slug: string;
  bids: BookLevel[]; // best (highest) first
  asks: BookLevel[]; // best (lowest) first
  state: string | null;
  transactTime: number | null;
}

export interface NormalizedSettlement {
  slug: string;
  /** settlement value of the LONG side in USD per contract: 1 = long won, 0 = long lost (0.5 seen in SDK tests) */
  value: number;
}

export function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Number from number | numeric string | Amount {value}. */
export function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string") {
    const s = v.replace(/[$,\s]/g, "");
    if (s === "") return null;
    const n = Number(s);
    return Number.isFinite(n) ? n : null;
  }
  if (isObj(v) && "value" in v) return num(v.value);
  return null;
}

/**
 * USD contract price (Amount, "0.55", 0.55) -> cents (55). Rounded to 0.1¢.
 * Defensive: a value > 1 is assumed to already be cents (the documented unit is dollars).
 */
export function toCents(v: unknown): number | null {
  const n = num(v);
  if (n === null || n < 0) return null;
  const cents = n > 1 ? n : n * 100;
  if (cents > 100) return null;
  return Math.round(cents * 10) / 10;
}

/** ISO-8601 / epoch (s or ms) -> epoch ms. */
export function toMs(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v < 1e12 ? v * 1000 : v;
  if (typeof v === "string" && v.trim() !== "") {
    if (/^\d+(\.\d+)?$/.test(v.trim())) return toMs(Number(v));
    const t = Date.parse(v);
    return Number.isFinite(t) ? t : null;
  }
  return null;
}

/** GET /v1/markets/{slug}/bbo -> { marketData: MarketBBO }. Also accepts a bare MarketBBO / marketDataLite. */
export function parseBbo(raw: unknown, slugHint = ""): NormalizedBbo | null {
  if (!isObj(raw)) return null;
  const md = isObj(raw.marketData) ? raw.marketData : isObj(raw.marketDataLite) ? raw.marketDataLite : raw;
  if (!("bestBid" in md) && !("bestAsk" in md) && !("lastTradePx" in md)) return null;
  return {
    slug: typeof md.marketSlug === "string" ? md.marketSlug : slugHint,
    bid: toCents(md.bestBid),
    ask: toCents(md.bestAsk),
    last: toCents(md.lastTradePx),
    bidDepth: num(md.bidDepth),
    askDepth: num(md.askDepth),
  };
}

function parseLevels(v: unknown): BookLevel[] {
  if (!Array.isArray(v)) return [];
  const out: BookLevel[] = [];
  for (const lvl of v) {
    if (!isObj(lvl)) continue;
    const price = toCents(lvl.px);
    const qty = num(lvl.qty);
    if (price === null || qty === null) continue;
    out.push({ price, qty });
  }
  return out;
}

/** GET /v1/markets/{slug}/book -> { marketData: MarketBook } (bids / offers of { px: Amount, qty: string }). */
export function parseBook(raw: unknown, slugHint = ""): NormalizedBook | null {
  if (!isObj(raw)) return null;
  const md = isObj(raw.marketData) ? raw.marketData : raw;
  if (!Array.isArray(md.bids) && !Array.isArray(md.offers)) return null;
  const bids = parseLevels(md.bids).sort((a, b) => b.price - a.price);
  const asks = parseLevels(md.offers).sort((a, b) => a.price - b.price);
  return {
    slug: typeof md.marketSlug === "string" ? md.marketSlug : slugHint,
    bids,
    asks,
    state: typeof md.state === "string" ? md.state : null,
    transactTime: toMs(md.transactTime),
  };
}

/** Top of a normalized book as a BBO. */
export function bookToBbo(book: NormalizedBook): NormalizedBbo {
  return {
    slug: book.slug,
    bid: book.bids[0]?.price ?? null,
    ask: book.asks[0]?.price ?? null,
    last: null,
    bidDepth: book.bids.length,
    askDepth: book.asks.length,
  };
}

/**
 * GET /v1/markets/{slug}/settlement -> { slug, settlement: number } (SDK >= 2.0, "numeric settlement").
 * Also tolerates the legacy shape { marketSlug, settlementPrice: Amount, settledAt }.
 * Returns null when the payload carries no settlement value (not settled yet).
 */
export function parseSettlement(raw: unknown, slugHint = ""): NormalizedSettlement | null {
  if (!isObj(raw)) return null;
  const slug =
    typeof raw.slug === "string" ? raw.slug : typeof raw.marketSlug === "string" ? raw.marketSlug : slugHint;
  const v = num(raw.settlement ?? raw.settlementPrice);
  if (v === null || v < 0) return null;
  const value = v > 1 ? v / 100 : v; // defensive: cents
  if (value > 1) return null;
  return { slug, value };
}

/** Long-side settlement value -> window result, for a window whose Up instrument is that market's long side. */
export function settlementToResult(value: number, upIsLong = true): "up" | "down" | null {
  if (value >= 0.99) return upIsLong ? "up" : "down";
  if (value <= 0.01) return upIsLong ? "down" : "up";
  return null; // 0.5 or other partial value: void / unknown, no Up/Down result
}

// ---- markets WebSocket ----

export type WsMarketMsg =
  | { kind: "bbo"; bbo: NormalizedBbo }
  | { kind: "book"; book: NormalizedBook }
  | { kind: "trade"; slug: string; price: number | null; qty: number | null; ts: number | null }
  | { kind: "heartbeat" }
  | { kind: "error"; error: string; requestId: string | null }
  | { kind: "unknown" };

/** One markets-WS frame (SDK src/websocket/types.ts MarketMessage). Never throws. */
export function parseWsMessage(text: string): WsMarketMsg {
  let m: unknown;
  try {
    m = JSON.parse(text);
  } catch {
    return { kind: "unknown" };
  }
  if (!isObj(m)) return { kind: "unknown" };
  if ("heartbeat" in m) return { kind: "heartbeat" };
  if (typeof m.error === "string" && m.error !== "") {
    return { kind: "error", error: m.error, requestId: typeof m.requestId === "string" ? m.requestId : null };
  }
  if (isObj(m.marketDataLite)) {
    const bbo = parseBbo({ marketDataLite: m.marketDataLite });
    if (bbo) return { kind: "bbo", bbo };
  }
  if (isObj(m.marketData)) {
    const book = parseBook({ marketData: m.marketData });
    if (book) return { kind: "book", book };
  }
  if (isObj(m.trade)) {
    const t = m.trade;
    return {
      kind: "trade",
      slug: typeof t.marketSlug === "string" ? t.marketSlug : "",
      price: toCents(t.price),
      qty: num(t.quantity),
      ts: toMs(t.tradeTime),
    };
  }
  return { kind: "unknown" };
}
