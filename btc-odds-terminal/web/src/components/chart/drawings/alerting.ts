// "Alert at this line" for horizontal lines (pure: no store, no network; unit-tested).
//
// The alert's direction comes from where the line sits relative to the *current* value, so it is only
// picked automatically from a fresh reference: a BTC tick a few seconds old (feed connected, no replay) or
// the current window's Up mid. With no fresh value, or a line right at the current value, the caller asks
// the user instead (New alert dialog, threshold prefilled).

import type { Alert, AlertInput, BtcTick, Drawing, OddsSnap } from "@shared/protocol";

export const TICK_FRESH_MS = 5_000;
export const ODDS_FRESH_MS = 15_000;
/** a line within this distance of the reference is "at" it (direction ambiguous) */
const SAME_PRICE_EPS = 0.005;

export interface AlertRefInput {
  connected: boolean;
  replaying: boolean;
  lastTick: BtcTick | null;
  /** the current window's quote (selectOdds), null when none */
  odds: OddsSnap | null;
}

/** current BTC price / Up mid, each null unless fresh */
export function alertRefs(s: AlertRefInput, now: number): { btc: number | null; upMid: number | null } {
  const live = s.connected && !s.replaying;
  const t = s.lastTick;
  const o = s.odds;
  return {
    btc: live && t && Number.isFinite(t.price) && now - t.ts <= TICK_FRESH_MS ? t.price : null,
    upMid: live && o && o.upMid != null && Number.isFinite(o.upMid) && now - o.ts <= ODDS_FRESH_MS ? o.upMid : null,
  };
}

/**
 * Price alert for a horizontal line: "above" when the line sits over the current value, "below" when
 * under it. BTC-scale lines alert on BTC, odds-scale lines on the Up mid. Null (ask the user) when there is
 * no current value or the line is at it.
 */
export function alertForLine(d: Drawing, btcNow: number | null, upMidNow: number | null): AlertInput | null {
  if (d.kind !== "hline") return null;
  const p = d.points[0];
  if (!p) return null;
  const ref = p.scale === "odds" ? upMidNow : btcNow;
  if (ref == null || Math.abs(p.price - ref) < SAME_PRICE_EPS) return null;
  const above = p.price > ref;
  return {
    kind: p.scale === "odds" ? (above ? "up_above" : "up_below") : above ? "btc_above" : "btc_below",
    threshold: p.price,
    enabled: true,
    once: true,
    note: d.label?.trim() ? d.label.trim() : null,
  };
}

/** an enabled alert of the same kind at the same threshold already exists */
export function duplicateAlert(alerts: readonly Alert[], input: AlertInput): Alert | null {
  return alerts.find((a) => a.enabled && a.kind === input.kind && a.threshold != null && input.threshold != null && Math.abs(a.threshold - input.threshold) < SAME_PRICE_EPS) ?? null;
}
