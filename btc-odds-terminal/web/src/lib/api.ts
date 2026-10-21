import type {
  Alert,
  AlertEvent,
  AlertInput,
  AnalysisPayload,
  AnalysisScope,
  ApiErrorBody,
  Bar,
  BookLevels,
  Bracket,
  BracketInput,
  ClosePositionRequest,
  Drawing,
  DrawingInput,
  FairSnap,
  Fill,
  KillResult,
  LiveConfirmResponse,
  LiveStatus,
  MarketWindow,
  OddsSnap,
  Order,
  OrderStatus,
  PlaceOrderRequest,
  PlaceOrderResponse,
  Position,
  ReplayData,
  RiskPatch,
  RiskState,
  Settings,
  SettingsPatch,
  StateSnapshot,
} from "@shared/protocol";
import { ApiError } from "./api-error";
import { API_URL, isMock } from "./env";
import { getMock } from "./mock";
import { mockRequest } from "./mock/router";
import type { HttpMethod } from "./mock/types";

export { ApiError, isApiError } from "./api-error";

/** REST calls give up after this long (a hung request must not block history loading forever). */
export const REQUEST_TIMEOUT_MS = 10_000;

async function request<T>(method: string, path: string, init: RequestInit = {}): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_URL}/api${path}`, { ...init, method, signal: ctl.signal });
    if (!res.ok) {
      let body: Partial<ApiErrorBody> = {};
      try {
        body = (await res.json()) as ApiErrorBody;
      } catch {}
      throw new ApiError(res.status, body.code ?? null, body.error ?? `${method} ${path} -> ${res.status}`);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

const get = <T>(path: string): Promise<T> => request<T>("GET", path, { cache: "no-store" });

/** Steps 3-6 calls: the real backend, or lib/mock/router.ts in mock mode. Throws ApiError. */
function call<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  if (isMock()) {
    // make sure the simulator (and its route table) exists
    getMock();
    return mockRequest<T>(method, path, body);
  }
  if (method === "GET") return get<T>(path);
  return request<T>(method, path, body === undefined ? {} : { headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

const qs = (q: Record<string, string | number | null | undefined>): string => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};
const enc = encodeURIComponent;

export const api = {
  state: (): Promise<StateSnapshot> => (isMock() ? Promise.resolve(getMock().snapshot()) : get("/state")),
  bars: (from: number, to: number): Promise<Bar[]> =>
    isMock() ? Promise.resolve(getMock().bars(from, to)) : get(`/history/bars?from=${from}&to=${to}`),
  odds: (from: number, to: number): Promise<OddsSnap[]> =>
    isMock() ? Promise.resolve(getMock().odds(from, to)) : get(`/history/odds?from=${from}&to=${to}`),
  fair: (from: number, to: number): Promise<FairSnap[]> =>
    isMock() ? Promise.resolve(getMock().fair(from, to)) : get(`/history/fair?from=${from}&to=${to}`),
  windows: (limit = 50): Promise<MarketWindow[]> =>
    isMock() ? Promise.resolve(getMock().windows(limit)) : get(`/windows?limit=${limit}`),
  settings: (): Promise<Settings> => (isMock() ? Promise.resolve(getMock().settings()) : get("/settings")),
  putSettings: async (patch: SettingsPatch): Promise<Settings> => {
    if (isMock()) return getMock().putSettings(patch);
    return request<Settings>("PUT", "/settings", {
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
  },

  // ---- book ----
  book: (): Promise<BookLevels | null> => call("GET", "/book"),

  // ---- trading ----
  placeOrder: (req: PlaceOrderRequest): Promise<PlaceOrderResponse> => call("POST", "/orders", req),
  cancelOrder: (id: string): Promise<Order> => call("DELETE", `/orders/${enc(id)}`),
  orders: (q: { limit?: number; windowId?: string; status?: OrderStatus } = {}): Promise<Order[]> => call("GET", `/orders${qs(q)}`),
  fills: (q: { limit?: number; windowId?: string } = {}): Promise<Fill[]> => call("GET", `/fills${qs(q)}`),
  positions: (q: { status?: "open" | "all"; limit?: number } = {}): Promise<Position[]> => call("GET", `/positions${qs(q)}`),
  closePosition: (id: string, req: ClosePositionRequest = {}): Promise<PlaceOrderResponse> =>
    call("POST", `/positions/${enc(id)}/close`, req),
  /** null removes the bracket */
  putBracket: (positionId: string, bracket: BracketInput | null): Promise<Bracket | null> =>
    call("PUT", `/positions/${enc(positionId)}/bracket`, bracket),
  brackets: (q: { status?: "active" | "all" } = {}): Promise<Bracket[]> => call("GET", `/brackets${qs(q)}`),

  // ---- risk ----
  kill: (): Promise<KillResult> => call("POST", "/kill", {}),
  risk: (): Promise<RiskState> => call("GET", "/risk"),
  putRisk: (patch: RiskPatch): Promise<RiskState> => call("PUT", "/risk", patch),

  // ---- drawings ----
  drawings: (): Promise<Drawing[]> => call("GET", "/drawings"),
  createDrawing: (d: DrawingInput): Promise<Drawing> => call("POST", "/drawings", d),
  updateDrawing: (id: string, patch: Partial<DrawingInput>): Promise<Drawing> => call("PUT", `/drawings/${enc(id)}`, patch),
  deleteDrawing: (id: string): Promise<{ ok: true }> => call("DELETE", `/drawings/${enc(id)}`),
  deleteAllDrawings: (): Promise<{ deleted: number }> => call("DELETE", "/drawings"),
  /** replace the whole list (undo / redo of bulk changes) */
  replaceDrawings: (list: Drawing[]): Promise<Drawing[]> => call("PUT", "/drawings", list),

  // ---- alerts ----
  alerts: (): Promise<Alert[]> => call("GET", "/alerts"),
  createAlert: (a: AlertInput): Promise<Alert> => call("POST", "/alerts", a),
  updateAlert: (id: string, patch: Partial<AlertInput>): Promise<Alert> => call("PUT", `/alerts/${enc(id)}`, patch),
  deleteAlert: (id: string): Promise<{ ok: true }> => call("DELETE", `/alerts/${enc(id)}`),
  alertEvents: (limit = 50): Promise<AlertEvent[]> => call("GET", `/alerts/events${qs({ limit })}`),

  // ---- analysis / replay ----
  analysis: (scope: AnalysisScope): Promise<AnalysisPayload> => call("GET", `/analysis${qs({ scope })}`),
  replay: (windowId: string): Promise<ReplayData> => call("GET", `/replay/${enc(windowId)}`),

  // ---- live ----
  liveStatus: (): Promise<LiveStatus> => call("GET", "/live/status"),
  liveConfirm: (): Promise<LiveConfirmResponse> => call("POST", "/live/confirm", {}),
};
