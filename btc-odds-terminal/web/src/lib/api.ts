import type { Bar, FairSnap, MarketWindow, OddsSnap, Settings, StateSnapshot } from "@shared/protocol";
import { API_URL, isMock } from "./env";
import { getMock } from "./mock";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}/api${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

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
    const res = await fetch(`${API_URL}/api/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw new Error(`PUT /settings -> ${res.status}`);
    return (await res.json()) as Settings;
  },
};
