import type { Bar, FairSnap, MarketWindow, OddsSnap, Settings, StateSnapshot } from "@shared/protocol";
import { API_URL, isMock } from "./env";
import { getMock } from "./mock";

/** REST calls give up after this long (a hung request must not block history loading forever). */
export const REQUEST_TIMEOUT_MS = 10_000;

async function request<T>(method: string, path: string, init: RequestInit = {}): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_URL}/api${path}`, { ...init, method, signal: ctl.signal });
    if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}`);
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

const get = <T>(path: string): Promise<T> => request<T>("GET", path, { cache: "no-store" });

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
  putSettings: async (patch: Partial<Settings>): Promise<Settings> => {
    if (isMock()) return getMock().putSettings(patch);
    return request<Settings>("PUT", "/settings", {
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
  },
};
