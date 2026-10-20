// Mock live mode: no exchange; "live" orders are simulated like paper but labeled live, so the settings toggle
// and the first-live-order confirm dialog are demoable. OWNER: live-backend.
// STUB from the scaffold (works): replace freely (keep `createMockLive` + MockLive).

import { ApiError } from "../api-error";
import type { LiveStatus } from "@shared/protocol";
import type { MockCtx, MockLive } from "./types";

export function createMockLive(ctx: MockCtx): MockLive {
  const sessions = new Map<string, number>();
  const status = (): LiveStatus => {
    const on = ctx.settings().mode === "live";
    return {
      available: true,
      enabled: on,
      connected: on,
      lastReconcileTs: on ? ctx.now() : null,
      balanceUsd: on ? 1000 : null,
      buyingPowerUsd: on ? 1000 : null,
      error: null,
    };
  };
  return {
    name: "live",
    status,
    isSessionConfirmed: (s) => !!s && (sessions.get(s) ?? 0) > ctx.now(),
    routes: [
      { method: "GET", path: "/live/status", handler: () => status() },
      {
        method: "POST",
        path: "/live/confirm",
        handler: () => {
          if (ctx.settings().mode !== "live") throw new ApiError(409, "live_unavailable", "switch settings to live mode first");
          const session = `mock-${Math.random().toString(36).slice(2)}`;
          const expiresTs = ctx.now() + 12 * 3600_000;
          sessions.set(session, expiresTs);
          return { session, expiresTs };
        },
      },
    ],
    onSettings: () => ctx.emit({ type: "live", data: status() }),
    snapshot: () => ({ live: status() }),
  };
}
