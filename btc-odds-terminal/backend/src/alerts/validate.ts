// Request validation for the alerts and risk routes (zod). OWNER: alerts-risk.

import { z } from "zod";
import type { AlertInput, AlertKind, RiskLimits } from "../../../shared/protocol.ts";
import { RiskLimitsPatchSchema } from "../db/settings.ts";
import { badRequest, zodError } from "../server/errors.ts";

export const ALERT_KINDS = [
  "btc_above",
  "btc_below",
  "up_above",
  "up_below",
  "down_above",
  "down_below",
  "edge_above",
  "edge_below",
  "btc_cross_ptb",
] as const satisfies readonly AlertKind[];

/** Most alerts one install may hold (each one is evaluated on every update). */
export const MAX_ALERTS = 200;
export const MAX_NOTE_LEN = 200;

const AlertFields = z
  .object({
    kind: z.enum(ALERT_KINDS),
    threshold: z.number().finite().nullable(),
    enabled: z.boolean(),
    once: z.boolean(),
    note: z.string().max(MAX_NOTE_LEN).nullable(),
  })
  .strict();

const CreateSchema = AlertFields.partial({ threshold: true, enabled: true, once: true, note: true });
const UpdateSchema = AlertFields.partial();

/** Threshold units / ranges per kind; btc_cross_ptb ignores the threshold (stored null). */
function checkThreshold(kind: AlertKind, threshold: number | null): number | null {
  if (kind === "btc_cross_ptb") return null;
  if (threshold == null) throw badRequest(`threshold: required for ${kind}`);
  if (kind.startsWith("btc_")) {
    if (!(threshold > 0 && threshold < 10_000_000)) throw badRequest("threshold: BTC price must be between $0 and $10,000,000 (USD)");
  } else if (kind.startsWith("edge_")) {
    if (!(threshold > -100 && threshold < 100)) throw badRequest("threshold: edge must be between -100 and 100 (cents)");
  } else if (!(threshold > 0 && threshold < 100)) {
    throw badRequest("threshold: odds must be between 0 and 100 (cents)");
  }
  return threshold;
}

const normNote = (n: string | null | undefined): string | null => {
  const t = n?.trim();
  return t ? t : null;
};

export function parseAlertInput(body: unknown): AlertInput {
  const r = CreateSchema.safeParse(body);
  if (!r.success) throw zodError(r.error.issues);
  const b = r.data;
  return {
    kind: b.kind,
    threshold: checkThreshold(b.kind, b.threshold ?? null),
    enabled: b.enabled ?? true,
    once: b.once ?? false,
    note: normNote(b.note),
  };
}

/** Partial update merged over `current`, validated as a whole. */
export function parseAlertUpdate(body: unknown, current: AlertInput): AlertInput {
  const r = UpdateSchema.safeParse(body ?? {});
  if (!r.success) throw zodError(r.error.issues);
  const b = r.data;
  const kind = b.kind ?? current.kind;
  return {
    kind,
    threshold: checkThreshold(kind, b.threshold !== undefined ? b.threshold : current.threshold),
    enabled: b.enabled ?? current.enabled,
    once: b.once ?? current.once,
    note: b.note !== undefined ? normNote(b.note) : current.note,
  };
}

const EventsQuery = z.object({
  limit: z.coerce.number().int().min(1).max(500).optional(),
  alertId: z.string().min(1).max(100).optional(),
});

export function parseEventsQuery(q: unknown): { limit: number; alertId?: string } {
  const r = EventsQuery.safeParse(q ?? {});
  if (!r.success) throw zodError(r.error.issues);
  return { limit: r.data.limit ?? 50, ...(r.data.alertId ? { alertId: r.data.alertId } : {}) };
}

// ---------------------------------------------------------------- risk

const RiskPatchSchema = RiskLimitsPatchSchema.extend({
  killSwitch: z.literal(false).optional(),
  clearDisable: z.literal(true).optional(),
}).strict();

export interface ParsedRiskPatch {
  limits: Partial<RiskLimits>;
  /** { killSwitch: false }: re-arm the kill switch */
  rearm: boolean;
  /** { clearDisable: true }: manual override of today's daily-loss disable (confirmed in the UI) */
  clearDisable: boolean;
}

/**
 * PUT /api/risk: limits (merged into Settings.risk), { killSwitch: false } to re-arm and/or
 * { clearDisable: true } to lift today's daily-loss disable. dailyLossLimitUsd must be > 0 (a cleared form
 * field sent as 0 would otherwise lock trading for the day).
 */
export function parseRiskPatch(body: unknown): ParsedRiskPatch {
  if (body == null || typeof body !== "object" || Array.isArray(body)) throw badRequest("body: must be a RiskPatch object");
  const r = RiskPatchSchema.safeParse(body);
  if (!r.success) {
    const ks: unknown = (body as Record<string, unknown>).killSwitch;
    if (ks === true) throw badRequest("killSwitch: use POST /api/kill to engage the kill switch");
    throw zodError(r.error.issues);
  }
  const { killSwitch, clearDisable, ...limits } = r.data;
  if (limits.dailyLossLimitUsd !== undefined && !(limits.dailyLossLimitUsd > 0)) {
    throw badRequest("dailyLossLimitUsd: must be greater than $0");
  }
  return { limits, rearm: killSwitch === false, clearDisable: clearDisable === true };
}
