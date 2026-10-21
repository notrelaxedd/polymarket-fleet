// Pure risk math shared by backend/src/risk and the web mock. OWNER: alerts-risk.
// The "day" for the daily loss limit is the America/New_York calendar day.

import type { RiskLimits, RiskState, TradingMode } from "./protocol";

const ET = "America/New_York";
const dayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit" });

/** "YYYY-MM-DD" of ts in America/New_York. */
export function etDay(ts: number): string {
  return dayFmt.format(new Date(ts));
}

/** First ms of the ET day after the one containing ts (DST-safe: probes hour by hour). */
export function nextEtMidnight(ts: number): number {
  const day = etDay(ts);
  let t = ts - (ts % 3_600_000) + 3_600_000;
  while (etDay(t) === day) t += 3_600_000;
  // back off to the exact boundary minute
  let lo = t - 3_600_000;
  let hi = t;
  while (hi - lo > 1000) {
    const mid = Math.floor((lo + hi) / 2);
    if (etDay(mid) === day) lo = mid;
    else hi = mid;
  }
  return Math.ceil(hi / 1000) * 1000;
}

/** Total (realized + unrealized) loss must stay at the daily limit this long before it latches the disable. */
export const DAILY_LOSS_CONFIRM_MS = 10_000;

/** Daily loss counted against the limit (>= 0): -(realized + unrealized when negative), fees included. */
export function dailyLoss(dailyRealizedPnlUsd: number, unrealizedPnlUsd: number): number {
  return Math.max(0, -(dailyRealizedPnlUsd + Math.min(0, unrealizedPnlUsd)));
}

/** A loss is at the limit (a zero loss never is, so a limit of 0 cannot lock trading with no loss). */
export function atLossLimit(loss: number, limitUsd: number): boolean {
  return loss > 1e-9 && loss >= limitUsd - 1e-9;
}

export interface RiskInputs {
  limits: RiskLimits;
  mode: TradingMode;
  now: number;
  dailyRealizedPnlUsd: number;
  /** unrealized P&L of open positions (counts toward the daily loss when negative) */
  unrealizedPnlUsd: number;
  openExposureUsd: number;
  windowLossUsedUsd: number;
  killSwitch: boolean;
  tradingDisabledUntil: number | null;
  disabledReason: string | null;
}

/**
 * Assemble RiskState from the inputs. It does NOT decide the daily-loss disable: the caller's policy passes
 * tradingDisabledUntil / disabledReason (backend/src/alerts/risk.ts, web mock lib/mock/risk.ts: realized loss at
 * the limit latches at once, a total incl. unrealized only after DAILY_LOSS_CONFIRM_MS; a zero loss never trips).
 * An expired disable (until <= now) is dropped.
 */
export function computeRiskState(i: RiskInputs): RiskState {
  const loss = dailyLoss(i.dailyRealizedPnlUsd, i.unrealizedPnlUsd);
  const until = i.tradingDisabledUntil != null && i.tradingDisabledUntil > i.now ? i.tradingDisabledUntil : null;
  const reason = until ? i.disabledReason : null;
  return {
    limits: i.limits,
    mode: i.mode,
    day: etDay(i.now),
    dailyRealizedPnlUsd: i.dailyRealizedPnlUsd,
    dailyLossUsedUsd: Math.round(loss * 100) / 100,
    openExposureUsd: i.openExposureUsd,
    windowLossUsedUsd: i.windowLossUsedUsd,
    tradingDisabledUntil: until,
    disabledReason: reason,
    killSwitch: i.killSwitch,
    updatedTs: i.now,
  };
}

export interface RiskOrderCheck {
  /** USD this order can add to exposure (buy: usd; exits: 0) */
  addCostUsd: number;
  /** cost already open in the order's window (position + resting buys) */
  windowCostUsd: number;
  /** cost of the existing position on the same side/window this order adds to */
  positionCostUsd: number;
  /** exits / closes / kill always pass (they reduce risk) */
  reducing: boolean;
}

export type RiskCheckResult = { ok: true } | { ok: false; code: "risk_rejected" | "trading_disabled"; reason: string };

/** Baseline pre-trade check; alerts-risk owns + tests it. */
export function checkOrder(s: RiskState, o: RiskOrderCheck, now: number): RiskCheckResult {
  if (o.reducing) return { ok: true };
  if (s.killSwitch) return { ok: false, code: "trading_disabled", reason: "Kill switch engaged" };
  if (s.tradingDisabledUntil != null && s.tradingDisabledUntil > now) {
    return { ok: false, code: "trading_disabled", reason: s.disabledReason ?? "Trading disabled" };
  }
  const L = s.limits;
  if (o.positionCostUsd + o.addCostUsd > L.maxPositionUsd + 1e-9)
    return { ok: false, code: "risk_rejected", reason: `Max position $${L.maxPositionUsd}` };
  if (s.openExposureUsd + o.addCostUsd > L.maxExposureUsd + 1e-9)
    return { ok: false, code: "risk_rejected", reason: `Max open exposure $${L.maxExposureUsd}` };
  if (o.windowCostUsd + o.addCostUsd > L.maxLossPerWindowUsd + 1e-9)
    return { ok: false, code: "risk_rejected", reason: `Max loss per window $${L.maxLossPerWindowUsd}` };
  return { ok: true };
}
