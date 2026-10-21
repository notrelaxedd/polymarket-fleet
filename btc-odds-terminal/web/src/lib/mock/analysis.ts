// Mock GET /api/analysis: runs @shared/analysis (the same math as the backend) on the simulator's history.
// OWNER: analysis. Scope sizes and payload TTLs mirror backend/src/analysis.

import type { AnalysisPayload, AnalysisScope } from "@shared/protocol";
import { computeAnalysis, validUpMid } from "@shared/analysis";
import { ApiError } from "../api-error";
import type { MockCtx, MockModule } from "./types";

const TTL_MS: Record<AnalysisScope, number> = { window: 2_000, last20: 5_000, all: 15_000 };
const LIVE_MAX_AGE_MS = 5_000;

export function createMockAnalysis(ctx: MockCtx): MockModule {
  const cache = new Map<AnalysisScope, AnalysisPayload>();
  return {
    name: "analysis",
    onRollover: () => cache.clear(),
    routes: [
      {
        method: "GET",
        path: "/analysis",
        handler: ({ query }) => {
          const scope = (query.scope ?? "last20") as AnalysisScope;
          if (!["window", "last20", "all"].includes(scope)) throw new ApiError(400, "validation", "bad scope");
          const now = ctx.now();
          const hit = cache.get(scope);
          if (hit && now - hit.computedTs >= 0 && now - hit.computedTs < TTL_MS[scope]) return hit;
          const cur = ctx.current();
          const started = ctx.windows(1000).filter((w) => w.startTs <= now);
          // last20 = the live window plus 19 finished ones (backend windowsFor)
          const wins = scope === "window" ? [cur] : scope === "last20" ? started.slice(0, 20) : started;
          if (wins.length === 0) throw new ApiError(409, "no_market", "no windows yet");
          const from = wins.reduce((m, w) => Math.min(m, w.startTs), Infinity);
          const tick = ctx.lastTick();
          const odds = ctx.lastOdds();
          const fair = ctx.lastFair();
          const live =
            tick && now - tick.ts <= LIVE_MAX_AGE_MS
              ? { ts: tick.ts, btc: tick.price, upMid: odds?.windowId === cur.id ? validUpMid(odds) : null, sigma: fair?.sigma ?? null, fair: fair?.fair ?? null }
              : null;
          const p = computeAnalysis({
            scope,
            now,
            windows: wins,
            bars: ctx.bars(from, now),
            odds: ctx.odds(from, now),
            fair: ctx.fair(from, now),
            current: cur,
            live,
          });
          cache.set(scope, p);
          return p;
        },
      },
    ],
  };
}
