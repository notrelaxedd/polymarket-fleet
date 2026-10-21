// Pure alert evaluation shared by backend/src/alerts and the web mock. OWNER: alerts-risk.

import type { Alert, AlertKind } from "./protocol";

/** Market values an alert is evaluated against (one sample per evaluation, ~1/s). */
export interface AlertSample {
  ts: number;
  windowId: string | null;
  btc: number | null;
  upMid: number | null;
  /** 100 - upMid when only the Up book is known */
  downMid: number | null;
  edge: number | null;
  ptb: number | null;
}

export const ALERT_KIND_LABELS: Record<AlertKind, string> = {
  btc_above: "BTC above",
  btc_below: "BTC below",
  up_above: "Up above",
  up_below: "Up below",
  down_above: "Down above",
  down_below: "Down below",
  edge_above: "Edge above",
  edge_below: "Edge below",
  btc_cross_ptb: "BTC crosses price to beat",
};

/** The observed value for an alert kind (btc_cross_ptb: BTC - PTB). */
export function alertValue(kind: AlertKind, s: AlertSample): number | null {
  switch (kind) {
    case "btc_above":
    case "btc_below":
      return s.btc;
    case "up_above":
    case "up_below":
      return s.upMid;
    case "down_above":
    case "down_below":
      return s.downMid;
    case "edge_above":
    case "edge_below":
      return s.edge;
    case "btc_cross_ptb":
      return s.btc != null && s.ptb != null ? s.btc - s.ptb : null;
  }
}

/**
 * Edge-triggered: fires when the value CROSSES the threshold between `prev` and `cur` (not on every sample
 * while beyond it). btc_cross_ptb fires on a sign change of BTC - PTB within the same window.
 * Simple two-sample form. The backend and the web mock use the full state machine in shared/alert-eval.ts
 * (re-arm band, cooldown, per-window reset, PTB dead band).
 */
export function alertFires(a: Pick<Alert, "kind" | "threshold" | "enabled">, prev: AlertSample | null, cur: AlertSample): boolean {
  if (!a.enabled || !prev) return false;
  const p = alertValue(a.kind, prev);
  const c = alertValue(a.kind, cur);
  if (p == null || c == null) return false;
  if (a.kind === "btc_cross_ptb") return prev.windowId === cur.windowId && Math.sign(p) !== Math.sign(c) && c !== 0;
  const t = a.threshold;
  if (t == null) return false;
  return a.kind.endsWith("_above") ? p < t && c >= t : p > t && c <= t;
}

export function alertMessage(a: Pick<Alert, "kind" | "threshold">, value: number | null): string {
  const v = value == null ? "—" : value.toFixed(2);
  if (a.kind === "btc_cross_ptb") return `BTC crossed the price to beat (Δ ${v})`;
  return `${ALERT_KIND_LABELS[a.kind]} ${a.threshold ?? "—"} (now ${v})`;
}
