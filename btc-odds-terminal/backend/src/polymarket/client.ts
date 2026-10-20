// Thin REST client for the Polymarket US public gateway (no auth).
// Paths verified against the official SDK (polymarket-us-typescript src/resources/*.ts):
//   GET /v1/events                 { events: Event[] }
//   GET /v1/events/slug/{slug}     { event: Event }
//   GET /v1/search?query=          { events: Event[] }  (events include nested markets)
//   GET /v1/markets                { markets: MarketDetail[] }
//   GET /v1/market/slug/{slug}     { market: MarketDetail }
//   GET /v1/markets/{slug}/bbo     { marketData: MarketBBO }
//   GET /v1/markets/{slug}/book    { marketData: MarketBook }
//   GET /v1/markets/{slug}/settlement  { slug, settlement: number }

import { loadPolyConfig } from "./config.ts";
import {
  isObj,
  parseBbo,
  parseBook,
  parseSettlement,
  type NormalizedBbo,
  type NormalizedBook,
  type NormalizedSettlement,
} from "./normalize.ts";

export class PolyHttpError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = "PolyHttpError";
  }
}

export type Query = Record<string, string | number | boolean | Array<string | number> | null | undefined>;

export interface ClientOptions {
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

/** Raw JSON records as returned by the gateway (fields beyond the SDK types are kept). */
export type RawEvent = Record<string, unknown>;
export type RawMarket = Record<string, unknown>;

export interface MarketDataSource {
  listEvents(q?: Query): Promise<RawEvent[]>;
  searchEvents(q: Query): Promise<RawEvent[]>;
  getEventBySlug(slug: string): Promise<RawEvent | null>;
  getBbo(slug: string): Promise<NormalizedBbo | null>;
  getSettlement(slug: string): Promise<NormalizedSettlement | null>;
}

export class PolymarketClient implements MarketDataSource {
  readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: ClientOptions = {}) {
    const cfg = loadPolyConfig();
    this.baseUrl = (opts.baseUrl ?? cfg.gatewayUrl).replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? cfg.timeoutMs;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  /** GET JSON; 404 -> null; other non-2xx -> PolyHttpError; timeout -> PolyHttpError(408). */
  async getJson(path: string, query?: Query): Promise<unknown> {
    const url = new URL(path, this.baseUrl + "/");
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v === undefined || v === null) continue;
        if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
        else url.searchParams.set(k, String(v));
      }
    }
    let res: Response;
    try {
      res = await this.fetchImpl(url.toString(), {
        method: "GET",
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      const e = err as Error;
      const timedOut = e?.name === "TimeoutError" || e?.name === "AbortError";
      throw new PolyHttpError(timedOut ? 408 : 0, path, timedOut ? `timeout ${path}` : `${path}: ${e?.message ?? e}`);
    }
    if (res.status === 404) return null;
    const text = await res.text();
    if (!res.ok) throw new PolyHttpError(res.status, path, `${res.status} ${path}: ${text.slice(0, 200)}`);
    if (!text) return {};
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new PolyHttpError(res.status, path, `${path}: invalid JSON`);
    }
  }

  async listEvents(q: Query = {}): Promise<RawEvent[]> {
    const j = await this.getJson("/v1/events", q);
    return isObj(j) && Array.isArray(j.events) ? j.events.filter(isObj) : [];
  }

  async searchEvents(q: Query): Promise<RawEvent[]> {
    const j = await this.getJson("/v1/search", q);
    return isObj(j) && Array.isArray(j.events) ? j.events.filter(isObj) : [];
  }

  async getEventBySlug(slug: string): Promise<RawEvent | null> {
    const j = await this.getJson(`/v1/events/slug/${encodeURIComponent(slug)}`);
    return isObj(j) && isObj(j.event) ? j.event : null;
  }

  async listMarkets(q: Query = {}): Promise<RawMarket[]> {
    const j = await this.getJson("/v1/markets", q);
    return isObj(j) && Array.isArray(j.markets) ? j.markets.filter(isObj) : [];
  }

  async getMarketBySlug(slug: string): Promise<RawMarket | null> {
    const j = await this.getJson(`/v1/market/slug/${encodeURIComponent(slug)}`);
    return isObj(j) && isObj(j.market) ? j.market : null;
  }

  async getBbo(slug: string): Promise<NormalizedBbo | null> {
    return parseBbo(await this.getJson(`/v1/markets/${encodeURIComponent(slug)}/bbo`), slug);
  }

  async getBook(slug: string): Promise<NormalizedBook | null> {
    return parseBook(await this.getJson(`/v1/markets/${encodeURIComponent(slug)}/book`), slug);
  }

  /** null = not settled yet (404 or no settlement value). */
  async getSettlement(slug: string): Promise<NormalizedSettlement | null> {
    return parseSettlement(await this.getJson(`/v1/markets/${encodeURIComponent(slug)}/settlement`), slug);
  }
}
