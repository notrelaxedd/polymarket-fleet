"use client";
// Trading calls shared by the right-panel cards: live-session handling (first live order of the browser session
// opens LiveConfirmDialog; a 409 live_confirm_required re-confirms once), optimistic store upserts of the REST
// result (the WS echo is an idempotent upsert), and user-facing error text. OWNER: trading-ui.
import type { ClosePositionRequest, Order, PlaceOrderRequest, PlaceOrderResponse, TradingMode } from "@shared/protocol";
import { api, isApiError } from "@/lib/api";
import { useTerminal } from "@/lib/store";
import { requestLiveConfirm } from "./LiveConfirmDialog";
import { newerOnly } from "./order-safety";

export class CancelledError extends Error {
  constructor() {
    super("Live order cancelled");
    this.name = "CancelledError";
  }
}

/**
 * Store the REST result right away so the panel does not wait for the WebSocket echo. The HTTP response and the
 * WS are separate connections: a record the WS already updated (e.g. a bracket exit closing the position right
 * after the entry) has a newer updatedTs and is kept.
 */
export function applyPlaceResponse(r: PlaceOrderResponse): void {
  const st = useTerminal.getState();
  const orders = newerOnly(st.orders, [r.order]);
  if (orders.length) st.upsertOrders(orders);
  if (r.fills.length) st.addFills(r.fills);
  if (r.position) {
    const p = newerOnly(st.positions, [r.position]);
    if (p.length) st.upsertPositions(p);
  }
  if (r.bracket) {
    const b = newerOnly(st.brackets, [r.bracket]);
    if (b.length) st.upsertBrackets(b);
  }
}

/** Refetch recent orders into the store (after a timed-out submit: did the first attempt land?). */
export async function refreshOrders(): Promise<Order[]> {
  const list = await api.orders({ limit: 50 });
  const st = useTerminal.getState();
  const fresh = newerOnly(st.orders, list);
  if (fresh.length) st.upsertOrders(fresh);
  return list;
}

/** Runs `call` with a confirmed live session when `mode` is live (null session in paper). */
async function withLiveSession<T>(mode: TradingMode, call: (session: string | null) => Promise<T>): Promise<T> {
  if (mode !== "live") return call(null);
  if (!(await requestLiveConfirm())) throw new CancelledError();
  try {
    return await call(useTerminal.getState().liveSession);
  } catch (e) {
    // session expired / backend restarted: ask again once
    if (isApiError(e) && e.code === "live_confirm_required") {
      useTerminal.getState().setLiveSession(null);
      if (!(await requestLiveConfirm())) throw new CancelledError();
      return call(useTerminal.getState().liveSession);
    }
    throw e;
  }
}

export async function submitOrder(mode: TradingMode, req: Omit<PlaceOrderRequest, "liveSession">): Promise<PlaceOrderResponse> {
  const r = await withLiveSession(mode, (liveSession) => api.placeOrder({ ...req, liveSession }));
  applyPlaceResponse(r);
  return r;
}

export async function closePosition(mode: TradingMode, id: string, req: Omit<ClosePositionRequest, "liveSession"> = {}): Promise<PlaceOrderResponse> {
  const r = await withLiveSession(mode, (liveSession) => api.closePosition(id, { ...req, liveSession }));
  applyPlaceResponse(r);
  return r;
}

/** Text for an error from a trading call (ApiError codes get a plain-language prefix). */
export function errorText(e: unknown): string {
  if (e instanceof CancelledError) return "Cancelled: live trading was not confirmed.";
  if (isApiError(e)) {
    switch (e.code) {
      case "risk_rejected":
        return `Blocked by risk limits: ${e.message}`;
      case "trading_disabled":
        return `Trading disabled: ${e.message}`;
      case "no_market":
        return `No market: ${e.message}`;
      case "live_unavailable":
        return `Live unavailable: ${e.message}`;
      case "exchange_error":
        return `Exchange error: ${e.message}`;
      default:
        return e.message;
    }
  }
  if (e instanceof Error && e.name === "AbortError") return "The server did not answer in time.";
  return e instanceof Error ? e.message : String(e);
}

/** Unique client order id (idempotency key for retries). */
export function newClientOrderId(): string {
  const r = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
  return `web-${r}`;
}
