// Pair-relationship analysis (scatter, lag, sensitivity). OWNER: analysis.
//
// Route: GET /api/analysis?scope=window|last20|all -> AnalysisPayload.
//
// The math is in ./compute.ts (pure). This module reads the recorded 1s bars / odds / fair rows per window,
// keeps a per-window partial (lag sums + compact scatter samples) for finished windows, which never change,
// and caches each scope's payload for a few seconds. "all" therefore costs one pass over history on the first
// request (done in chunks so the event loop keeps serving) and only the live window afterwards.

import type { AnalysisPayload, AnalysisScope, Bar, MarketWindow } from "../../../shared/protocol.ts";
import type { AnalysisService, BackendModule, ModuleDeps } from "../modules/types.ts";
import { badRequest } from "../server/errors.ts";
import {
  BTC_FILL_SEC,
  CURRENT_MAX_AGE_MS,
  assembleAnalysis,
  buildSeries,
  validUpMid,
  windowPartial,
  type LivePoint,
  type WindowPartial,
  type WindowSeries,
} from "./compute.ts";

const SCOPES: AnalysisScope[] = ["window", "last20", "all"];

/** Payload cache per scope. */
export const SCOPE_TTL_MS: Record<AnalysisScope, number> = { window: 2_000, last20: 5_000, all: 15_000 };
/** Windows in "last20" in total: the live one (when there is one) counts towards the 20. */
export const LAST_N_WINDOWS = 20;
/** Upper bound for "all" (~31 days of 15-min windows, the odds/fair retention). */
export const ALL_MAX_WINDOWS = 3_000;
/** A window's partial is final (cacheable) this long after its end (the recorder flushes every ~1s). */
export const FINAL_AFTER_MS = 15_000;
/** Per-window partial cache size (LRU). */
const PARTIAL_CACHE_MAX = ALL_MAX_WINDOWS + 100;
/** Yield to the event loop after this much synchronous work while warming "all". */
const YIELD_EVERY_MS = 20;
/** Warm the finished-window cache ("all") this long after start, so the first request is fast. */
const WARM_DELAY_MS = 5_000;

interface CachedPartial {
  key: string;
  partial: WindowPartial;
}

export function createAnalysis(deps: ModuleDeps): BackendModule<AnalysisService> {
  const { db, engine } = deps;
  // 1s bars only (the scatter / lag grids are per second; seeded 60s bars carry no 1s returns)
  const bars1s = db.raw.prepare("SELECT t, d, o, h, l, c FROM bars WHERE d = 1 AND t >= ? AND t < ? ORDER BY t");

  const partials = new Map<string, CachedPartial>();
  const payloads = new Map<AnalysisScope, AnalysisPayload>();
  const inflight = new Map<AnalysisScope, Promise<AnalysisPayload>>();

  const partialKey = (w: MarketWindow) => `${w.startTs}|${w.endTs}|${w.priceToBeat ?? ""}`;
  const isFinal = (w: MarketWindow, now: number) => w.endTs + FINAL_AFTER_MS <= now;

  function loadSeries(w: MarketWindow, now: number): WindowSeries | null {
    const until = Math.min(w.endTs, now);
    if (until <= w.startTs) return null;
    const from = w.startTs - BTC_FILL_SEC * 1000;
    return buildSeries(
      {
        window: w,
        bars: bars1s.all(from, until + 1) as Bar[],
        odds: db.odds(from, until, w.id),
        fair: db.fair(from, until, w.id),
      },
      now,
    );
  }

  function cachedPartial(w: MarketWindow): WindowPartial | undefined {
    const c = partials.get(w.id);
    if (!c || c.key !== partialKey(w)) return undefined;
    // LRU touch
    partials.delete(w.id);
    partials.set(w.id, c);
    return c.partial;
  }

  function computePartial(w: MarketWindow, now: number): { partial: WindowPartial | null; series: WindowSeries | null } {
    const series = loadSeries(w, now);
    const partial = series ? windowPartial(series) : null;
    if (partial && isFinal(w, now)) {
      partials.delete(w.id);
      partials.set(w.id, { key: partialKey(w), partial });
      while (partials.size > PARTIAL_CACHE_MAX) partials.delete(partials.keys().next().value!);
    }
    return { partial, series };
  }

  /** Windows of a scope (the engine's current window object wins over the DB row). */
  function windowsFor(scope: AnalysisScope, now: number): { windows: MarketWindow[]; current: MarketWindow | null } {
    const live = engine.currentWindow();
    const current = live && live.startTs <= now && now < live.endTs ? live : null;
    let list: MarketWindow[];
    if (scope === "window") {
      // nothing live (between windows / no market): fall back to the most recent finished window
      list = current ? [current] : db.recentFinishedWindows(1, now);
    } else if (scope === "last20") {
      list = [...(current ? [current] : []), ...db.recentFinishedWindows(LAST_N_WINDOWS, now)];
    } else {
      list = [...(current ? [current] : []), ...db.listWindows(ALL_MAX_WINDOWS)];
    }
    const seen = new Set<string>();
    const out: MarketWindow[] = [];
    for (const w of list) {
      if (seen.has(w.id) || w.startTs > now) continue;
      seen.add(w.id);
      out.push(current && w.id === current.id ? current : w);
    }
    if (scope === "last20") {
      // 20 windows: the live one + the 19 finished before it (or the last 20 finished when nothing is live)
      return { windows: out.slice(0, LAST_N_WINDOWS), current };
    }
    return { windows: out.slice(0, ALL_MAX_WINDOWS), current };
  }

  function livePoint(now: number): LivePoint | null {
    const tick = engine.latestTick();
    if (!tick || now - tick.ts > CURRENT_MAX_AGE_MS) return null;
    const odds = engine.latestOdds(now);
    const fair = engine.latestFair(now);
    return { ts: tick.ts, btc: tick.price, upMid: validUpMid(odds), sigma: fair?.sigma ?? null, fair: fair?.fair ?? null };
  }

  function build(scope: AnalysisScope, now: number): AnalysisPayload {
    const { windows, current } = windowsFor(scope, now);
    const parts: WindowPartial[] = [];
    let currentSeries: WindowSeries | null = null;
    for (const w of windows) {
      const final = isFinal(w, now);
      const hit = final ? cachedPartial(w) : undefined;
      if (hit) {
        parts.push(hit);
        continue;
      }
      const { partial, series } = computePartial(w, now);
      if (partial) parts.push(partial);
      if (current && w.id === current.id) currentSeries = series;
    }
    return assembleAnalysis({ scope, now, windows, partials: parts, current, currentSeries, live: livePoint(now) });
  }

  function fresh(scope: AnalysisScope, now: number): AnalysisPayload | null {
    const p = payloads.get(scope);
    return p && now - p.computedTs >= 0 && now - p.computedTs < SCOPE_TTL_MS[scope] ? p : null;
  }

  function compute(scope: AnalysisScope): AnalysisPayload {
    const now = deps.now();
    const hit = fresh(scope, now);
    if (hit) return hit;
    const p = build(scope, now);
    payloads.set(scope, p);
    return p;
  }

  /** Warm the per-window cache for a scope in chunks (yielding between them), then compute. */
  async function computeAsync(scope: AnalysisScope): Promise<AnalysisPayload> {
    const hit = fresh(scope, deps.now());
    if (hit) return hit;
    const running = inflight.get(scope);
    if (running) return running;
    const job = (async () => {
      const now = deps.now();
      const { windows } = windowsFor(scope, now);
      let sliceStart = Date.now();
      for (const w of windows) {
        if (!isFinal(w, now) || cachedPartial(w)) continue;
        computePartial(w, now);
        if (Date.now() - sliceStart >= YIELD_EVERY_MS) {
          await new Promise<void>((r) => setImmediate(r));
          sliceStart = Date.now();
        }
      }
      return compute(scope);
    })();
    inflight.set(scope, job);
    try {
      return await job;
    } finally {
      inflight.delete(scope);
    }
  }

  // a rollover changes what "this window" / "last 20" mean: drop the payload cache
  const onRollover = () => payloads.clear();
  let warmTimer: NodeJS.Timeout | null = null;

  const service: AnalysisService = { compute };
  return {
    name: "analysis",
    service,
    register(app) {
      app.get("/api/analysis", async (req) => {
        const raw = (req.query as Record<string, unknown> | undefined)?.scope ?? "last20";
        const scope = raw as AnalysisScope;
        if (typeof raw !== "string" || !SCOPES.includes(scope)) throw badRequest("scope must be window, last20 or all");
        return computeAsync(scope);
      });
    },
    start() {
      engine.on("rollover", onRollover);
      warmTimer = setTimeout(() => {
        warmTimer = null;
        const t = Date.now();
        computeAsync("all").then(
          (p) => deps.logger.info({ windows: p.windowIds.length, ms: Date.now() - t }, "analysis cache warmed"),
          (err) => deps.logger.warn({ err }, "analysis warm-up failed"),
        );
      }, WARM_DELAY_MS);
      warmTimer.unref();
    },
    stop() {
      if (warmTimer) clearTimeout(warmTimer);
      warmTimer = null;
      engine.off("rollover", onRollover);
      payloads.clear();
      partials.clear();
    },
  };
}
