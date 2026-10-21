// Alerts: CRUD, backend evaluation on every tick / odds / fair update, alert_events log. OWNER: alerts-risk.
//
// Routes: GET/POST /api/alerts, PUT/DELETE /api/alerts/:id, GET /api/alerts/events?limit=50[&alertId=].
// Broadcasts: `alerts` (full list) on every change (CRUD, lastFiredTs, `once` auto-disable) and `alertFired`
// (AlertEvent) when one fires; the browser turns alertFired into a notification + sound.
// Evaluation runs in the worker on each engine "tick" / "odds" / "fair" event, so alerts fire with no browser
// open. Trigger rules (crossing, hysteresis re-arm band, cooldown) live in ./evaluator.ts.
//
// Inputs per sample: BTC = engine.latestTick() if <= BTC_MAX_AGE_MS old; Up mid / Down mid from
// engine.latestOdds() (current window only, null when stale; Down mid = 100 - Up mid when the Down book has
// no two-sided quote); edge from engine.latestFair(); PTB of the current window while it is trading.

import { randomUUID } from "node:crypto";
import type { Alert, AlertEvent, OddsSnap } from "../../../shared/protocol.ts";
import type { AlertSample } from "../../../shared/alerts.ts";
import type { AlertsService, BackendModule, ModuleDeps } from "../modules/types.ts";
import { badRequest, notFound } from "../server/errors.ts";
import { AlertEvaluator, type FiredAlert } from "./evaluator.ts";
import { firedMessage } from "./message.ts";
import { AlertStore } from "./store.ts";
import { MAX_ALERTS, parseAlertInput, parseAlertUpdate, parseEventsQuery } from "./validate.ts";

export { ALERT_COOLDOWN_MS, AlertEvaluator, hysteresisBand } from "./evaluator.ts";

/** A BTC tick older than this is not evaluated (feed stale). */
export const BTC_MAX_AGE_MS = 5_000;
/** alert_events pruning cadence. */
export const ALERT_PRUNE_INTERVAL_MS = 3_600_000;
/** Snapshot / default listing size. */
export const SNAPSHOT_EVENTS = 50;

const mid = (bid: number | null, ask: number | null): number | null =>
  bid != null && ask != null ? (bid + ask) / 2 : null;

/** Down mid: the Down book's two-sided mid, else the complement of the Up mid. */
export function downMidOf(o: OddsSnap): number | null {
  return mid(o.downBid, o.downAsk) ?? (o.upMid != null ? 100 - o.upMid : null);
}

export interface AlertsModule extends BackendModule<AlertsService> {
  /** evaluate every enabled alert against the engine's latest values now; returns the events fired */
  evaluate(): AlertEvent[];
  /** the sample evaluate() would use (exposed for tests / debugging) */
  sample(): AlertSample;
}

export function createAlerts(deps: ModuleDeps): AlertsModule {
  const store = new AlertStore(deps.db.raw);
  const evaluator = new AlertEvaluator();
  /** id -> alert, in creation order (Map keeps insertion order) */
  const alerts = new Map<string, Alert>(store.all().map((a) => [a.id, a]));
  let pruneTimer: NodeJS.Timeout | null = null;

  const list = (): Alert[] => [...alerts.values()].map((a) => ({ ...a }));
  const publishList = () => deps.broadcast({ type: "alerts", data: list() });

  const sample = (): AlertSample => {
    const now = deps.now();
    const e = deps.engine;
    const cur = e.currentWindow();
    const tick = e.latestTick();
    const odds = e.latestOdds(now);
    const fair = e.latestFair(now);
    const trading = cur != null && now >= cur.startTs && now < cur.endTs;
    return {
      ts: now,
      windowId: cur?.id ?? null,
      btc: tick && Number.isFinite(tick.price) && now - tick.ts <= BTC_MAX_AGE_MS ? tick.price : null,
      upMid: odds?.upMid ?? null,
      downMid: odds ? downMidOf(odds) : null,
      edge: fair?.edge ?? null,
      ptb: trading ? cur.priceToBeat : null,
    };
  };

  const record = (f: FiredAlert, s: AlertSample): AlertEvent => {
    const cur = alerts.get(f.alert.id)!;
    const message = firedMessage(f, s.ptb);
    // persist a copy; the in-memory alert changes only once the transaction committed (a failed write leaves
    // memory and DB in step: lastFiredTs / `once` untouched)
    const a: Alert = { ...cur, lastFiredTs: f.ts, enabled: cur.once ? false : cur.enabled, updatedTs: f.ts };
    const ev = store.tx(() => {
      const inserted = store.insertEvent({ alertId: a.id, kind: a.kind, ts: f.ts, windowId: f.windowId, value: f.value, message });
      store.update(a);
      return inserted;
    });
    if (alerts.get(a.id) === cur) alerts.set(a.id, a);
    if (!a.enabled) evaluator.reset(a.id);
    deps.logger.info({ alertId: a.id, kind: a.kind, value: f.value, windowId: f.windowId }, `alert fired: ${message}`);
    return ev;
  };

  const evaluate = (): AlertEvent[] => {
    if (alerts.size === 0) return [];
    let enabled = false;
    for (const a of alerts.values()) if (a.enabled) enabled = true;
    if (!enabled) return [];
    const s = sample();
    const fired = evaluator.evaluate(alerts.values(), s);
    if (!fired.length) return [];
    const out: AlertEvent[] = [];
    for (const f of fired) {
      try {
        out.push(record(f, s));
      } catch (err) {
        deps.logger.error({ err, alertId: f.alert.id }, "alert event write failed");
      }
    }
    for (const ev of out) deps.broadcast({ type: "alertFired", data: ev });
    if (out.length) {
      publishList();
      if (out.some((e) => e.id % 100 === 0)) prune();
    }
    return out;
  };

  const onUpdate = () => {
    try {
      evaluate();
    } catch (err) {
      deps.logger.error({ err }, "alert evaluation failed");
    }
  };

  const prune = () => {
    try {
      const n = store.prune(deps.now());
      if (n) deps.logger.debug({ deleted: n }, "alert events pruned");
    } catch (err) {
      deps.logger.warn({ err }, "alert events prune failed");
    }
  };

  const get = (id: string): Alert => {
    const a = alerts.get(id);
    if (!a) throw notFound("alert not found");
    return a;
  };

  const service: AlertsService = {
    list,
    events: (limit = SNAPSHOT_EVENTS) => store.events(limit),
  };

  return {
    name: "alerts",
    service,
    evaluate,
    sample,
    register(app) {
      app.get("/api/alerts", async () => list());

      app.get("/api/alerts/events", async (req) => {
        const q = parseEventsQuery(req.query);
        return store.events(q.limit, q.alertId);
      });

      app.post("/api/alerts", async (req) => {
        const input = parseAlertInput(req.body);
        if (alerts.size >= MAX_ALERTS) throw badRequest(`at most ${MAX_ALERTS} alerts; delete some first`);
        const now = deps.now();
        const a: Alert = { id: randomUUID(), ...input, lastFiredTs: null, createdTs: now, updatedTs: now };
        store.insert(a);
        alerts.set(a.id, a);
        publishList();
        return { ...a };
      });

      app.put<{ Params: { id: string } }>("/api/alerts/:id", async (req) => {
        const cur = get(req.params.id);
        const next = parseAlertUpdate(req.body, cur);
        const a: Alert = { ...cur, ...next, updatedTs: deps.now() };
        store.update(a);
        alerts.set(a.id, a);
        // a new definition (or re-enable) starts from a fresh arm state: it must cross again to fire
        if (a.kind !== cur.kind || a.threshold !== cur.threshold || a.enabled !== cur.enabled) evaluator.reset(a.id);
        publishList();
        return { ...a };
      });

      app.delete<{ Params: { id: string } }>("/api/alerts/:id", async (req) => {
        get(req.params.id);
        store.remove(req.params.id);
        alerts.delete(req.params.id);
        evaluator.reset(req.params.id);
        publishList();
        return { ok: true as const };
      });
    },
    start() {
      deps.engine.on("tick", onUpdate);
      deps.engine.on("odds", onUpdate);
      deps.engine.on("fair", onUpdate);
      prune();
      pruneTimer = setInterval(prune, ALERT_PRUNE_INTERVAL_MS);
      pruneTimer.unref?.();
    },
    stop() {
      deps.engine.off("tick", onUpdate);
      deps.engine.off("odds", onUpdate);
      deps.engine.off("fair", onUpdate);
      if (pruneTimer) clearInterval(pruneTimer);
      pruneTimer = null;
    },
    snapshot: () => ({ alerts: list(), alertEvents: store.events(SNAPSHOT_EVENTS) }),
  };
}
