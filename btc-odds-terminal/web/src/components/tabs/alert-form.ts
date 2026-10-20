// Pure helpers for the Alerts tab and NewAlertDialog: condition text, units, form <-> AlertInput, validation.
// OWNER: tabs. Tested in src/components/tabs/__tests__/tabs-logic.test.ts.

import type { Alert, AlertInput, AlertKind } from "@shared/protocol";
import { ALERT_KIND_LABELS } from "@shared/alerts";
import { btc } from "@/lib/format";

export type AlertUnit = "usd" | "cents" | "none";

export function alertUnit(kind: AlertKind): AlertUnit {
  if (kind === "btc_cross_ptb") return "none";
  return kind.startsWith("btc_") ? "usd" : "cents";
}

/** Kinds grouped for the dialog's <select>. */
export const ALERT_KIND_GROUPS: { label: string; kinds: AlertKind[] }[] = [
  { label: "BTC · BRTI proxy", kinds: ["btc_above", "btc_below", "btc_cross_ptb"] },
  { label: "Up odds (mid)", kinds: ["up_above", "up_below"] },
  { label: "Down odds (mid)", kinds: ["down_above", "down_below"] },
  { label: "Edge · fair − market", kinds: ["edge_above", "edge_below"] },
];

export const ALL_ALERT_KINDS: AlertKind[] = ALERT_KIND_GROUPS.flatMap((g) => g.kinds);

const trimNum = (v: number, digits: number) => {
  const s = v.toFixed(digits);
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
};

/** "$67,250.00" / "60¢" / "−3.5¢" ("—" when null) */
export function formatThreshold(kind: AlertKind, v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const unit = alertUnit(kind);
  if (unit === "usd") return `$${btc(v)}`;
  if (unit === "cents") {
    const sign = v < 0 ? "−" : kind.startsWith("edge_") && v > 0 ? "+" : "";
    return `${sign}${trimNum(Math.abs(v), 1)}¢`;
  }
  return "";
}

/** "BTC above $67,250.00" / "Up below 40¢" / "BTC crosses price to beat" */
export function alertCondition(a: Pick<Alert, "kind" | "threshold">): string {
  if (a.kind === "btc_cross_ptb") return ALERT_KIND_LABELS.btc_cross_ptb;
  return `${ALERT_KIND_LABELS[a.kind]} ${formatThreshold(a.kind, a.threshold)}`;
}

// ---- form ----

export interface AlertForm {
  kind: AlertKind;
  /** raw input text */
  threshold: string;
  enabled: boolean;
  once: boolean;
  note: string;
}

export const DEFAULT_ALERT_FORM: AlertForm = { kind: "btc_above", threshold: "", enabled: true, once: false, note: "" };

/** Text for a threshold input (no grouping separators, no unit). */
export function thresholdText(kind: AlertKind, v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || alertUnit(kind) === "none") return "";
  return trimNum(v, alertUnit(kind) === "usd" ? 2 : 1);
}

export function formFromInput(p: Partial<AlertInput> | null | undefined, base: AlertForm = DEFAULT_ALERT_FORM): AlertForm {
  if (!p) return { ...base };
  const kind = p.kind && ALL_ALERT_KINDS.includes(p.kind) ? p.kind : base.kind;
  return {
    kind,
    threshold: p.threshold !== undefined ? thresholdText(kind, p.threshold) : base.threshold,
    enabled: p.enabled ?? base.enabled,
    once: p.once ?? base.once,
    note: p.note ?? base.note,
  };
}

export const NOTE_MAX = 120;

/** Parse "67,250.5" / "$67250" / "−3" / "60¢" -> number (null when not a number). */
export function parseThreshold(text: string): number | null {
  const t = text.trim().replace(/[$,¢\s]/g, "").replace(/^[−–]/, "-");
  if (!t || !/^[+-]?(\d+\.?\d*|\.\d+)$/.test(t)) return null;
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

export type AlertFormResult = { ok: true; input: AlertInput } | { ok: false; field: "threshold" | "note"; error: string };

export function validateAlertForm(f: AlertForm): AlertFormResult {
  const unit = alertUnit(f.kind);
  const note = f.note.trim();
  if (note.length > NOTE_MAX) return { ok: false, field: "note", error: `Keep the note under ${NOTE_MAX} characters.` };
  let threshold: number | null = null;
  if (unit !== "none") {
    const v = parseThreshold(f.threshold);
    if (v == null) return { ok: false, field: "threshold", error: unit === "usd" ? "Enter a BTC price in USD." : "Enter a value in cents." };
    if (unit === "usd" && (v <= 0 || v >= 10_000_000)) return { ok: false, field: "threshold", error: "Enter a BTC price above $0." };
    if (f.kind.startsWith("edge_")) {
      if (v <= -100 || v >= 100) return { ok: false, field: "threshold", error: "Edge must be between −99¢ and +99¢." };
    } else if (unit === "cents" && (v <= 0 || v >= 100)) {
      return { ok: false, field: "threshold", error: "Odds must be between 0¢ and 100¢ (exclusive)." };
    }
    threshold = unit === "usd" ? Math.round(v * 100) / 100 : Math.round(v * 10) / 10;
  }
  return { ok: true, input: { kind: f.kind, threshold, enabled: f.enabled, once: f.once, note: note || null } };
}

/**
 * PUT body for an edit: only the fields that differ from the alert as the dialog opened it, so a backend
 * change made meanwhile (a `once` alert auto-disabled when it fired, broadcast while the dialog was open) is
 * not overwritten with the stale value. kind and threshold always travel together (validated as a pair).
 */
export function alertEditPatch(initial: AlertInput, edited: AlertInput): Partial<AlertInput> {
  const p: Partial<AlertInput> = {};
  if (edited.kind !== initial.kind || edited.threshold !== initial.threshold) {
    p.kind = edited.kind;
    p.threshold = edited.threshold;
  }
  if (edited.enabled !== initial.enabled) p.enabled = edited.enabled;
  if (edited.once !== initial.once) p.once = edited.once;
  if (edited.note !== initial.note) p.note = edited.note;
  return p;
}

// ---- current values ----

export interface LiveValues {
  btc: number | null;
  upMid: number | null;
  edge: number | null;
  ptb: number | null;
}

/** The live value an alert kind watches (btc_cross_ptb: BTC − PTB). */
export function liveValueFor(kind: AlertKind, v: LiveValues): number | null {
  switch (alertUnit(kind)) {
    case "usd":
      return v.btc;
    case "none":
      return v.btc != null && v.ptb != null ? v.btc - v.ptb : null;
    default:
      if (kind.startsWith("up_")) return v.upMid;
      if (kind.startsWith("down_")) return v.upMid == null ? null : 100 - v.upMid;
      return v.edge;
  }
}

/**
 * Alerts fire on a crossing, not while a value is already beyond the threshold. Returns a hint when the
 * threshold is already on the "fired" side so the user isn't surprised by silence.
 */
export function crossingHint(kind: AlertKind, threshold: number | null, current: number | null): string | null {
  if (threshold == null || current == null || kind === "btc_cross_ptb") return null;
  const above = kind.endsWith("_above");
  if (above && current >= threshold) return "Already above — it fires the next time the value crosses up through it.";
  if (!above && current <= threshold) return "Already below — it fires the next time the value crosses down through it.";
  return null;
}
