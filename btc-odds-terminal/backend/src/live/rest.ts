// Ed25519-signed REST client for api.polymarket.us (trading, portfolio, account).
// Paths and bodies VERIFIED against the official SDK (polymarket-us-typescript @abdcaae, src/resources/*.ts):
//   POST /v1/orders                      CreateOrderParams          -> { id, executions? }
//   GET  /v1/orders/open?slugs=          -                          -> { orders: Order[] }
//   GET  /v1/order/{id}                  -                          -> { order }
//   POST /v1/order/{id}/cancel           { marketSlug }             -> {}
//   POST /v1/orders/open/cancel          { slugs? }                 -> { canceledOrderIds }
//   POST /v1/order/close-position        { marketSlug, ... }        -> { id, executions? }
//   GET  /v1/portfolio/positions?market=&limit=&cursor=             -> { positions: Record<slug, UserPosition>, nextCursor?, eof? }
//   GET  /v1/account/balances                                       -> { balances: UserBalance[] }
// Signing (src/auth.ts): message = timestampMs + METHOD + pathname (no query, no body), base64 Ed25519.
//
// Order placement never retries: the API has no client order id, so a retried create is a second order. After an
// ambiguous failure (timeout / network / 5xx / a 2xx with an unreadable or unusable body) the caller resolves the
// outcome from open orders + positions instead.
// Secrets: the key id and secret are only passed to createAuthHeaders; nothing here logs headers or bodies.

import { createAuthHeaders, decodeSecret } from "../polymarket/auth.ts";
import { parseRetryAfter } from "../polymarket/client.ts";
import { isObj } from "../polymarket/normalize.ts";
import {
  parseExecution,
  parseOrder,
  parsePositionsPage,
  pickUsdBalance,
  type CreateOrderBody,
  type WireBalance,
  type WireExecution,
  type WireOrder,
  type WirePosition,
} from "./wire.ts";

export interface PositionsRead {
  positions: WirePosition[];
  /** slugs whose row could not be parsed (skipped) */
  unparseable: string[];
}

/** The order id of a POST /v1/orders (or close-position) response: `id`, tolerating `orderId` / `order.id`. */
function responseOrderId(j: unknown): string | null {
  if (!isObj(j)) return null;
  for (const v of [j.id, j.orderId, isObj(j.order) ? j.order.id : undefined]) if (typeof v === "string" && v !== "") return v;
  return null;
}

export class LiveHttpError extends Error {
  constructor(
    readonly status: number,
    readonly method: string,
    readonly path: string,
    message: string,
    readonly retryAfterMs: number | null = null,
    /** set for a 2xx whose body could not be read or used: the exchange accepted the request */
    private readonly forceAmbiguous = false,
  ) {
    super(message);
    this.name = "LiveHttpError";
  }

  /** the request may or may not have taken effect (no / unreadable / unusable response, timeout, 5xx) */
  get ambiguous(): boolean {
    return this.forceAmbiguous || this.status === 0 || this.status === 408 || this.status >= 500;
  }
}

export interface LiveRestOptions {
  baseUrl: string;
  keyId: string;
  secretKey: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

export type Query = Record<string, string | number | boolean | Array<string | number> | null | undefined>;

/** Signing uses the local clock corrected by the server's Date header once the skew exceeds this. */
const SKEW_CORRECT_MS = 2000;
const MAX_POSITION_PAGES = 20;

export class LiveRestClient {
  readonly baseUrl: string;
  private readonly keyId: string;
  private readonly secretKey: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => number;
  /** server time - local time, ms (0 until a response showed a skew > SKEW_CORRECT_MS) */
  clockOffsetMs = 0;

  constructor(opts: LiveRestOptions) {
    decodeSecret(opts.secretKey); // fail fast on a malformed secret (the message never includes the value)
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.keyId = opts.keyId;
    this.secretKey = opts.secretKey;
    this.timeoutMs = opts.timeoutMs ?? 5000;
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.now = opts.now ?? Date.now;
  }

  /** Signed request; returns parsed JSON ({} for an empty body). Non-2xx -> LiveHttpError. */
  async request(method: "GET" | "POST" | "DELETE", path: string, opts: { query?: Query; body?: unknown } = {}): Promise<unknown> {
    const url = new URL(path, this.baseUrl + "/");
    if (opts.query) {
      for (const [k, v] of Object.entries(opts.query)) {
        if (v === undefined || v === null) continue;
        if (Array.isArray(v)) for (const item of v) url.searchParams.append(k, String(item));
        else url.searchParams.set(k, String(v));
      }
    }
    const auth = await createAuthHeaders(this.keyId, this.secretKey, method, url.pathname, this.now() + this.clockOffsetMs);
    const transportError = (err: unknown, phase: string) => {
      const e = err as Error;
      const timedOut = e?.name === "TimeoutError" || e?.name === "AbortError";
      return new LiveHttpError(
        timedOut ? 408 : 0,
        method,
        url.pathname,
        timedOut ? `timeout ${method} ${url.pathname}${phase}` : `${method} ${url.pathname}${phase}: ${e?.message ?? e}`,
      );
    };
    let res: Response;
    try {
      res = await this.fetchImpl(url.toString(), {
        method,
        headers: { "Content-Type": "application/json", accept: "application/json", ...auth },
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      throw transportError(err, "");
    }
    this.trackSkew(res.headers.get("date"));
    // the timeout signal also covers the body: a stalled / reset body after the status line is as ambiguous as no
    // response at all (the exchange may have acted on the request)
    let text: string;
    try {
      text = await res.text();
    } catch (err) {
      throw transportError(err, " (reading the response body)");
    }
    if (!res.ok) {
      throw new LiveHttpError(
        res.status,
        method,
        url.pathname,
        `${res.status} ${method} ${url.pathname}: ${errorText(text, res.statusText)}`,
        parseRetryAfter(res.headers.get("retry-after"), this.now()),
      );
    }
    if (!text) return {};
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new LiveHttpError(res.status, method, url.pathname, `${method} ${url.pathname}: invalid JSON`, null, true);
    }
  }

  private trackSkew(dateHeader: string | null): void {
    if (!dateHeader) return;
    const server = Date.parse(dateHeader);
    if (!Number.isFinite(server)) return;
    // the Date header has 1s resolution: correct only a clear skew, and keep a corrected clock until it recovers
    const skew = server + 500 - this.now();
    this.clockOffsetMs = Math.abs(skew) > SKEW_CORRECT_MS ? skew : 0;
  }

  // ---------------------------------------------------------------- orders

  async createOrder(body: CreateOrderBody): Promise<{ id: string; executions: WireExecution[] }> {
    const j = await this.request("POST", "/v1/orders", { body });
    const id = responseOrderId(j);
    // a 2xx without a usable id: the order was accepted, we just cannot name it -> ambiguous, never "rejected"
    if (!id) throw new LiveHttpError(200, "POST", "/v1/orders", "POST /v1/orders: response without an order id", null, true);
    const executions = isObj(j) && Array.isArray(j.executions) ? j.executions.map(parseExecution).filter((e): e is WireExecution => e !== null) : [];
    return { id, executions };
  }

  async openOrders(slugs?: string[]): Promise<WireOrder[]> {
    const j = await this.request("GET", "/v1/orders/open", { query: { slugs: slugs && slugs.length > 0 ? slugs : undefined } });
    return isObj(j) && Array.isArray(j.orders) ? j.orders.map(parseOrder).filter((o): o is WireOrder => o !== null) : [];
  }

  /** null = 404 (unknown order) */
  async getOrder(id: string): Promise<WireOrder | null> {
    try {
      const j = await this.request("GET", `/v1/order/${encodeURIComponent(id)}`);
      return isObj(j) ? parseOrder(j.order) : null;
    } catch (err) {
      if (err instanceof LiveHttpError && err.status === 404) return null;
      throw err;
    }
  }

  async cancelOrder(id: string, marketSlug: string): Promise<void> {
    await this.request("POST", `/v1/order/${encodeURIComponent(id)}/cancel`, { body: { marketSlug } });
  }

  async cancelAll(slugs?: string[]): Promise<string[]> {
    const j = await this.request("POST", "/v1/orders/open/cancel", { body: slugs && slugs.length > 0 ? { slugs } : {} });
    return isObj(j) && Array.isArray(j.canceledOrderIds) ? j.canceledOrderIds.filter((x): x is string => typeof x === "string") : [];
  }

  async closePosition(marketSlug: string): Promise<{ id: string | null; executions: WireExecution[] }> {
    const j = await this.request("POST", "/v1/order/close-position", {
      body: { marketSlug, manualOrderIndicator: "MANUAL_ORDER_INDICATOR_MANUAL", synchronousExecution: true },
    });
    return {
      id: responseOrderId(j),
      executions: isObj(j) && Array.isArray(j.executions) ? j.executions.map(parseExecution).filter((e): e is WireExecution => e !== null) : [],
    };
  }

  // ---------------------------------------------------------------- portfolio / account

  /**
   * Every position (or one market's), following nextCursor until eof (at most MAX_POSITION_PAGES pages). Rows with an
   * unparseable netPosition are skipped and listed in `unparseable` (the caller decides whether they matter).
   */
  async positions(opts: { market?: string } = {}): Promise<PositionsRead> {
    const out: WirePosition[] = [];
    const unparseable: string[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < MAX_POSITION_PAGES; page++) {
      const j = await this.request("GET", "/v1/portfolio/positions", { query: { market: opts.market, limit: 100, cursor } });
      const p = parsePositionsPage(j);
      out.push(...p.positions);
      unparseable.push(...p.unparseable);
      if (p.eof || !p.nextCursor || p.nextCursor === cursor || p.positions.length + p.unparseable.length === 0) return { positions: out, unparseable };
      cursor = p.nextCursor;
    }
    throw new Error(`positions: more than ${MAX_POSITION_PAGES} pages`);
  }

  async balance(): Promise<WireBalance | null> {
    const j = await this.request("GET", "/v1/account/balances");
    return isObj(j) ? pickUsdBalance(j.balances) : null;
  }
}

function errorText(text: string, statusText: string): string {
  let msg = text;
  try {
    const j = JSON.parse(text) as unknown;
    if (isObj(j)) msg = String(j.message ?? j.error ?? text);
  } catch {
    /* not JSON */
  }
  return (msg || statusText || "error").replace(/\s+/g, " ").slice(0, 200);
}
