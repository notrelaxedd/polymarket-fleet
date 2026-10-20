// Mock alerts mirroring backend/src/alerts: CRUD with the same validation, evaluation every simulated second with
// the backend's own state machine (shared/alert-eval.ts: crossing only, re-arm band, 30s cooldown, per-window
// reset, PTB dead band), `once` alerts switching off, `alertFired` + `alerts` broadcasts. OWNER: alerts-risk.

import type { Alert, AlertEvent, AlertInput, AlertKind } from "@shared/protocol";
import { AlertEvaluator, firedMessage } from "@shared/alert-eval";
import type { AlertSample } from "@shared/alerts";
import { ApiError } from "../api-error";
import type { MockAlerts, MockCtx } from "./types";

const KINDS: AlertKind[] = ["btc_above", "btc_below", "up_above", "up_below", "down_above", "down_below", "edge_above", "edge_below", "btc_cross_ptb"];
const MAX_ALERTS = 200;
const MAX_EVENTS = 500;
const BTC_MAX_AGE_MS = 5_000;
const bad = (m: string) => new ApiError(400, "validation", m);

function checkThreshold(kind: AlertKind, threshold: number | null): number | null {
  if (kind === "btc_cross_ptb") return null;
  if (threshold == null || typeof threshold !== "number" || !Number.isFinite(threshold)) throw bad(`threshold: required for ${kind}`);
  if (kind.startsWith("btc_")) {
    if (!(threshold > 0 && threshold < 10_000_000)) throw bad("threshold: BTC price must be between $0 and $10,000,000 (USD)");
  } else if (kind.startsWith("edge_")) {
    if (!(threshold > -100 && threshold < 100)) throw bad("threshold: edge must be between -100 and 100 (cents)");
  } else if (!(threshold > 0 && threshold < 100)) throw bad("threshold: odds must be between 0 and 100 (cents)");
  return threshold;
}

function parse(body: unknown, cur: AlertInput | null): AlertInput {
  if (body == null || typeof body !== "object" || Array.isArray(body)) throw bad("body: must be an object");
  const b = body as Partial<AlertInput>;
  for (const k of Object.keys(b)) if (!["kind", "threshold", "enabled", "once", "note"].includes(k)) throw bad(`${k}: unknown field`);
  const kind = b.kind ?? cur?.kind;
  if (!kind || !KINDS.includes(kind)) throw bad("kind: invalid");
  if (b.enabled !== undefined && typeof b.enabled !== "boolean") throw bad("enabled: boolean");
  if (b.once !== undefined && typeof b.once !== "boolean") throw bad("once: boolean");
  if (b.note != null && (typeof b.note !== "string" || b.note.length > 200)) throw bad("note: at most 200 characters");
  const note = b.note !== undefined ? b.note?.trim() || null : (cur?.note ?? null);
  return {
    kind,
    threshold: checkThreshold(kind, b.threshold !== undefined ? b.threshold : (cur?.threshold ?? null)),
    enabled: b.enabled ?? cur?.enabled ?? true,
    once: b.once ?? cur?.once ?? false,
    note,
  };
}

export function createMockAlerts(ctx: MockCtx): MockAlerts {
  const t = ctx.now();
  const btc = ctx.lastTick()?.price ?? 67000;
  let seq = 0;
  let alerts: Alert[] = [
    { id: "a-ptb", kind: "btc_cross_ptb", threshold: null, enabled: true, once: false, note: null, lastFiredTs: null, createdTs: t, updatedTs: t },
    {
      id: "a-btc",
      kind: "btc_above",
      threshold: Math.round((btc + 150) / 50) * 50,
      enabled: true,
      once: true,
      note: null,
      lastFiredTs: null,
      createdTs: t,
      updatedTs: t,
    },
  ];
  const events: AlertEvent[] = []; // newest first
  let eventId = 0;
  const evaluator = new AlertEvaluator();
  const changed = () => ctx.emit({ type: "alerts", data: alerts.map((a) => ({ ...a })) });
  const find = (id: string) => {
    const a = alerts.find((x) => x.id === id);
    if (!a) throw new ApiError(404, "not_found", "no such alert");
    return a;
  };

  const sample = (): AlertSample => {
    const now = ctx.now();
    const cur = ctx.current();
    const tick = ctx.lastTick();
    const odds = ctx.lastOdds();
    const fair = ctx.lastFair();
    const oddsOk = odds && odds.windowId === cur.id && now - odds.ts <= 3_000 ? odds : null;
    const downMid = oddsOk
      ? oddsOk.downBid != null && oddsOk.downAsk != null
        ? (oddsOk.downBid + oddsOk.downAsk) / 2
        : oddsOk.upMid != null
          ? 100 - oddsOk.upMid
          : null
      : null;
    const trading = now >= cur.startTs && now < cur.endTs;
    return {
      ts: now,
      windowId: cur.id,
      btc: tick && now - tick.ts <= BTC_MAX_AGE_MS ? tick.price : null,
      upMid: oddsOk?.upMid ?? null,
      downMid,
      edge: fair && fair.windowId === cur.id && now - fair.ts <= 3_000 ? fair.edge : null,
      ptb: trading ? cur.priceToBeat : null,
    };
  };

  const evaluate = () => {
    if (!alerts.some((a) => a.enabled)) return;
    const s = sample();
    const fired = evaluator.evaluate(alerts, s);
    if (!fired.length) return;
    for (const f of fired) {
      const ev: AlertEvent = { id: ++eventId, alertId: f.alert.id, kind: f.alert.kind, ts: f.ts, windowId: f.windowId, value: f.value, message: firedMessage(f, s.ptb) };
      events.unshift(ev);
      alerts = alerts.map((a) => (a.id === f.alert.id ? { ...a, lastFiredTs: f.ts, enabled: a.once ? false : a.enabled, updatedTs: f.ts } : a));
      if (f.alert.once) evaluator.reset(f.alert.id);
      ctx.emit({ type: "alertFired", data: ev });
    }
    if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
    changed();
  };

  return {
    name: "alerts",
    list: () => alerts,
    events: (limit = 50) => events.slice(0, limit),
    onSecond: () => evaluate(),
    routes: [
      { method: "GET", path: "/alerts", handler: () => alerts },
      {
        method: "GET",
        path: "/alerts/events",
        handler: ({ query }) => {
          const limit = Math.min(500, Math.max(1, Number(query.limit ?? 50) || 50));
          return events.filter((e) => !query.alertId || e.alertId === query.alertId).slice(0, limit);
        },
      },
      {
        method: "POST",
        path: "/alerts",
        handler: ({ body }) => {
          if (alerts.length >= MAX_ALERTS) throw bad(`at most ${MAX_ALERTS} alerts`);
          const input = parse(body, null);
          const now = ctx.now();
          const a: Alert = { ...input, id: `a-${now.toString(36)}-${(seq++).toString(36)}`, lastFiredTs: null, createdTs: now, updatedTs: now };
          alerts = [...alerts, a];
          changed();
          return a;
        },
      },
      {
        method: "PUT",
        path: "/alerts/:id",
        handler: ({ params, body }) => {
          const cur = find(params.id);
          const a: Alert = { ...cur, ...parse(body, cur), updatedTs: ctx.now() };
          alerts = alerts.map((x) => (x.id === a.id ? a : x));
          evaluator.reset(a.id);
          changed();
          return a;
        },
      },
      {
        method: "DELETE",
        path: "/alerts/:id",
        handler: ({ params }) => {
          find(params.id);
          alerts = alerts.filter((x) => x.id !== params.id);
          evaluator.reset(params.id);
          changed();
          return { ok: true };
        },
      },
    ],
    snapshot: () => ({ alerts: alerts.map((a) => ({ ...a })), alertEvents: events.slice(0, 50) }),
  };
}
