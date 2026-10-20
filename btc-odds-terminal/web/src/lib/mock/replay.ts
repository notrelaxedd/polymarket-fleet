// Mock GET /api/replay/:windowId from the simulator's history, shaped like backend/src/replay: 15 minutes of 1s
// bars before the window as context, ticks thinned to 4/s, the window's odds / fair, per-second frames
// (shared/replay.ts buildFrames, the backend's own) and my fills. OWNER: replay.

import type { ReplayData } from "@shared/protocol";
import { buildFrames, thinTicks } from "@shared/replay";
import { ApiError } from "../api-error";
import type { MockCtx, MockModule } from "./types";

/** backend REPLAY_LEAD_MS */
export const LEAD_MS = 15 * 60_000;

export function createMockReplay(ctx: MockCtx): MockModule {
  return {
    name: "replay",
    routes: [
      {
        method: "GET",
        path: "/replay/:windowId",
        handler: ({ params }): ReplayData => {
          const w = ctx.getWindow(params.windowId);
          if (!w) throw new ApiError(404, "not_found", "no such window");
          const now = ctx.now();
          if (w.startTs > now) throw new ApiError(409, "no_market", "this window has not started yet");
          const end = Math.min(w.endTs, now);
          const bars = ctx.bars(w.startTs - LEAD_MS, end).filter((b) => b.d === 1);
          const odds = ctx.odds(w.startTs, end).filter((o) => o.windowId === w.id);
          const fair = ctx.fair(w.startTs, end).filter((f) => f.windowId === w.id);
          return {
            window: w,
            bars,
            odds,
            fair,
            frames: buildFrames(bars, odds, fair, w.startTs, end),
            fills: [...ctx.modules.trading.fills(w.id)].sort((a, b) => a.ts - b.ts),
            ticks: thinTicks(ctx.ticks(w.startTs - LEAD_MS, end)),
          };
        },
      },
    ],
  };
}
