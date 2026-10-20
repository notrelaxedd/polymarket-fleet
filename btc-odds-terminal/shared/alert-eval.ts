// Alert trigger state machine: crossing detection with hysteresis (re-arm band) and a per-alert cooldown.
// OWNER: alerts-risk. Pure (no I/O), shared by backend/src/alerts and the web mock (lib/mock/alerts.ts), which feed
// it one AlertSample per update. Extensionless imports (tsx + Next.js).
//
// Semantics (shared with the web mock's shared/alerts.ts alertFires(): an alert fires on a CROSSING, never on
// every sample while beyond the threshold):
//   - threshold kinds (*_above / *_below): an alert is ARMED while the value is on the near side of the threshold.
//     It fires when an armed alert's value reaches the threshold (>= for above, <= for below) and is then
//     disarmed. It re-arms only after the value retreats past the threshold by the hysteresis band
//     (hysteresisBand()), so noise around the level cannot fire it repeatedly.
//   - the first sample after an alert is created / enabled / edited only initialises the arm state: an alert
//     whose condition already holds waits for the value to come back and cross again.
//   - btc_cross_ptb: tracks which side of the price to beat BTC is on, with a dead band of PTB_BAND_USD around
//     the PTB; fires when the side flips within one window. A new window (new PTB) re-initialises the side, and
//     so does a PTB revised within the window (e.g. the BRTI proxy replaced by the metadata PTB): the side is
//     re-read against the new PTB without firing, since BTC did not move.
//   - up_* / down_* / edge_* are per-window instruments: a window change re-initialises the arm state, so the
//     jump from one market's odds to the next market's is never treated as a crossing.
//   - cooldown: a crossing within ALERT_COOLDOWN_MS of the alert's last fire is suppressed (consumed without
//     firing); lastFiredTs is persisted, so the cooldown survives restarts.
//   - a sample without the alert's value (stale feed, no PTB yet) changes nothing.

import type { Alert, AlertKind } from "./protocol";
import { ALERT_KIND_LABELS, alertValue, type AlertSample } from "./alerts";

/** Minimum gap between two fires of one alert. */
export const ALERT_COOLDOWN_MS = 30_000;
/** Dead band around the price to beat for btc_cross_ptb, USD. */
export const PTB_BAND_USD = 3;
/** Re-arm band for odds (Up / Down mid) and edge alerts, cents. */
export const ODDS_BAND_CENTS = 1;
/** Re-arm band for BTC price alerts: this fraction of the threshold, at least BTC_BAND_MIN_USD. */
export const BTC_BAND_FRACTION = 0.0002;
export const BTC_BAND_MIN_USD = 5;

/** How far the value must retreat past the threshold before the alert re-arms. */
export function hysteresisBand(kind: AlertKind, threshold: number | null): number {
  switch (kind) {
    case "btc_above":
    case "btc_below":
      return Math.max(BTC_BAND_MIN_USD, Math.abs(threshold ?? 0) * BTC_BAND_FRACTION);
    case "btc_cross_ptb":
      return PTB_BAND_USD;
    default:
      return ODDS_BAND_CENTS;
  }
}

const perWindow = (kind: AlertKind) => kind !== "btc_above" && kind !== "btc_below";

interface ArmState {
  /** threshold kinds: true = armed; btc_cross_ptb: unused */
  armed: boolean;
  /** btc_cross_ptb: last known side of the PTB outside the dead band (1 above, -1 below, 0 unknown) */
  side: -1 | 0 | 1;
  windowId: string | null;
  /** btc_cross_ptb: the PTB the side was measured against */
  ptb: number | null;
  /** the alert definition the state was built for (edits re-initialise) */
  key: string;
}

export interface FiredAlert {
  alert: Alert;
  /** the observed value: BTC price (btc_* and btc_cross_ptb), mid cents (up_ / down_), edge cents */
  value: number;
  /** btc_cross_ptb: the direction BTC crossed the PTB */
  direction?: "above" | "below";
  ts: number;
  windowId: string | null;
}

export type EvalOutcome = "fired" | "suppressed" | null;

export class AlertEvaluator {
  private readonly state = new Map<string, ArmState>();

  constructor(private readonly cooldownMs = ALERT_COOLDOWN_MS) {}

  /** Forget an alert's arm state (deleted / edited / disabled); the next sample re-initialises it. */
  reset(id: string): void {
    this.state.delete(id);
  }

  clear(): void {
    this.state.clear();
  }

  /** Evaluate every enabled alert against one sample; returns the ones that fire (caller persists them). */
  evaluate(alerts: Iterable<Alert>, sample: AlertSample): FiredAlert[] {
    const out: FiredAlert[] = [];
    const seen = new Set<string>();
    for (const a of alerts) {
      seen.add(a.id);
      if (!a.enabled) {
        this.state.delete(a.id);
        continue;
      }
      const f = this.step(a, sample);
      if (f) out.push(f);
    }
    for (const id of this.state.keys()) if (!seen.has(id)) this.state.delete(id);
    return out;
  }

  /** One alert, one sample. Exposed for tests. */
  step(a: Alert, s: AlertSample): FiredAlert | null {
    const v = alertValue(a.kind, s);
    if (v == null || !Number.isFinite(v)) return null;
    const key = `${a.kind}|${a.threshold}`;
    let st = this.state.get(a.id);
    if (st && (st.key !== key || (perWindow(a.kind) && st.windowId !== s.windowId))) st = undefined;

    if (a.kind === "btc_cross_ptb") return this.stepPtb(a, s, v, st, key);

    const t = a.threshold;
    if (t == null || !Number.isFinite(t)) return null;
    const above = a.kind.endsWith("_above");
    const reached = above ? v >= t : v <= t;
    if (!st) {
      this.state.set(a.id, { armed: !reached, side: 0, windowId: s.windowId, ptb: null, key });
      return null;
    }
    const band = hysteresisBand(a.kind, t);
    if (st.armed && reached) {
      st.armed = false;
      return this.coolingDown(a, s.ts) ? null : { alert: a, value: this.reported(a, s, v), ts: s.ts, windowId: s.windowId };
    }
    if (!st.armed && (above ? v < t - band : v > t + band)) st.armed = true;
    return null;
  }

  private stepPtb(a: Alert, s: AlertSample, diff: number, st: ArmState | undefined, key: string): FiredAlert | null {
    const side: -1 | 0 | 1 = diff > PTB_BAND_USD ? 1 : diff < -PTB_BAND_USD ? -1 : 0;
    if (!st) {
      this.state.set(a.id, { armed: false, side, windowId: s.windowId, ptb: s.ptb, key });
      return null;
    }
    if (st.ptb !== s.ptb) {
      // PTB revised mid-window: re-measure the side against the new PTB without firing
      st.ptb = s.ptb;
      st.side = side;
      return null;
    }
    if (side === 0 || side === st.side) return null;
    const prev = st.side;
    st.side = side;
    if (prev === 0 || this.coolingDown(a, s.ts)) return null;
    return {
      alert: a,
      value: s.btc ?? diff,
      direction: side > 0 ? "above" : "below",
      ts: s.ts,
      windowId: s.windowId,
    };
  }

  private coolingDown(a: Alert, ts: number): boolean {
    return a.lastFiredTs != null && ts - a.lastFiredTs < this.cooldownMs && ts >= a.lastFiredTs;
  }

  private reported(a: Alert, s: AlertSample, v: number): number {
    return a.kind === "btc_above" || a.kind === "btc_below" ? (s.btc ?? v) : v;
  }
}

// ---------------------------------------------------------------- messages
// Human sentences for alert events (shown in the Alerts tab, the browser notification and the log).

const usdFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const fmtUsd = (v: number) => usdFmt.format(v);
export const fmtCents = (v: number) => `${(Math.round(v * 10) / 10).toFixed(1)}¢`;
export const fmtSignedCents = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtCents(Math.abs(v))}`;

/** "BTC above $70,000.00", "Up below 40.0¢", "Edge above +5.0¢", "BTC crosses price to beat". */
export function describeAlert(a: Pick<Alert, "kind" | "threshold">): string {
  const label = ALERT_KIND_LABELS[a.kind];
  const t = a.threshold;
  if (a.kind === "btc_cross_ptb" || t == null) return label;
  if (a.kind.startsWith("btc_")) return `${label} ${fmtUsd(t)}`;
  if (a.kind.startsWith("edge_")) return `${label} ${fmtSignedCents(t)}`;
  return `${label} ${fmtCents(t)}`;
}

/** e.g. "BTC crossed above price to beat $67,000.00 (now $67,012.50)", "Up above 60.0¢ (mid 60.5¢) · note". */
export function firedMessage(f: FiredAlert, ptb: number | null): string {
  const a = f.alert;
  let msg: string;
  if (a.kind === "btc_cross_ptb") {
    const to = ptb != null ? ` ${fmtUsd(ptb)}` : "";
    msg = `BTC crossed ${f.direction ?? "over"} price to beat${to} (now ${fmtUsd(f.value)})`;
  } else if (a.kind.startsWith("btc_")) {
    msg = `${describeAlert(a)} (now ${fmtUsd(f.value)})`;
  } else if (a.kind.startsWith("edge_")) {
    msg = `${describeAlert(a)} (now ${fmtSignedCents(f.value)})`;
  } else {
    msg = `${describeAlert(a)} (mid ${fmtCents(f.value)})`;
  }
  const note = a.note?.trim();
  return note ? `${msg} · ${note}` : msg;
}
