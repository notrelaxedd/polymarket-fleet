// Replay data for one recorded window: bars (with lead-in context) + ticks + odds + fair + per-second frames
// + my fills in that window. OWNER: replay.
//
// Route: GET /api/replay/:windowId -> ReplayPayload (ReplayData + `ticks`)
//   404 not_found  unknown window
//   409 no_market  the window has not started yet (nothing recorded)
// A window that is still live replays up to now. Ticks are thinned to <= 4/s (per-exchange overlay); they are
// only retained for RETENTION.ticksMs, so older windows come back with `ticks: []` (the client falls back to
// the 1s bars).

import type { BtcTick, MarketWindow, ReplayData } from "../../../shared/protocol.ts";
import type { BackendModule, ModuleDeps, ReplayService } from "../modules/types.ts";
import { ApiError, notFound } from "../server/errors.ts";
import { buildFrames, thinTicks } from "./frames.ts";

/** History before the window start included in a replay (chart context: the whole previous window). */
export const REPLAY_LEAD_MS = 15 * 60_000;
/** Most fills returned for one window (markers). */
export const REPLAY_FILLS_LIMIT = 500;

/** ReplayData plus the window's proxy ticks (an extension of shared ReplayData; web lib/replay.ts reads it when
 *  present, clients that do not know it ignore it). */
export type ReplayPayload = ReplayData & { ticks: BtcTick[] };

export function createReplay(deps: ModuleDeps): BackendModule<ReplayService> {
  const findWindow = (id: string): MarketWindow | null => {
    const w = deps.db.getWindow(id);
    if (w) return w;
    // the live window may not have been flushed to the DB yet
    const cur = deps.engine.currentWindow();
    return cur && cur.id === id ? cur : null;
  };

  const loadPayload = (windowId: string): ReplayPayload | null => {
    const w = findWindow(windowId);
    if (!w) return null;
    const end = Math.min(w.endTs, deps.now());
    if (end < w.startTs) return { window: w, bars: [], ticks: [], odds: [], fair: [], frames: [], fills: [] };
    const from = w.startTs - REPLAY_LEAD_MS;
    const bars = deps.db.bars(from, end);
    const odds = deps.db.odds(w.startTs, end, w.id);
    const fair = deps.db.fair(w.startTs, end, w.id);
    return {
      window: w,
      bars,
      ticks: thinTicks(deps.db.ticks(from, end)),
      odds,
      fair,
      frames: buildFrames(bars, odds, fair, w.startTs, end),
      fills: deps.services.trading.fills({ windowId: w.id, limit: REPLAY_FILLS_LIMIT }).sort((a, b) => a.ts - b.ts),
    };
  };

  const service: ReplayService = {
    load: (windowId) => loadPayload(windowId),
  };

  return {
    name: "replay",
    service,
    register(app) {
      app.get("/api/replay/:windowId", async (req) => {
        const id = (req.params as { windowId: string }).windowId;
        const w = findWindow(id);
        if (!w) throw notFound(`no such window: ${id}`);
        if (w.startTs > deps.now()) throw new ApiError(409, "no_market", "window has not started yet");
        return loadPayload(id)!;
      });
    },
  };
}
