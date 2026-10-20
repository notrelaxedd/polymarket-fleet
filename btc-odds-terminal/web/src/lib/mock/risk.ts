// Mock risk controls mirroring backend/src/alerts/risk.ts. OWNER: alerts-risk.
//   - limits checked before every non-reducing order (shared/risk.ts checkOrder);
//   - daily loss: the REALIZED loss at the limit latches the disable at once; a total incl. unrealized must stay
//     at the limit DAILY_LOSS_CONFIRM_MS first (entries are paused meanwhile); a zero loss never trips. The
//     disable lasts until the next America/New_York midnight unless lifted with PUT /api/risk {clearDisable:true}
//     (refused while today's loss is still at the limit). Latching cancels the mode's resting buys;
//   - kill switch: engages first, cancels all, flattens, and stays on until PUT /api/risk {killSwitch:false};
//     PUT {killSwitch:true} is a 400 (use POST /api/kill); dailyLossLimitUsd <= 0 is a 400.

import type { RiskLimits, RiskPatch, RiskState, TradingMode } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { atLossLimit, checkOrder, computeRiskState, DAILY_LOSS_CONFIRM_MS, dailyLoss, etDay, nextEtMidnight } from "@shared/risk";
import { ApiError } from "../api-error";
import { etTime, usd } from "../format";
import type { MockCtx, MockRisk } from "./types";

const bad = (m: string) => new ApiError(400, "validation", m);
const LIMIT_KEYS = ["dailyLossLimitUsd", "maxExposureUsd", "maxLossPerWindowUsd", "maxPositionUsd"] as const;

export function createMockRisk(ctx: MockCtx): MockRisk {
  let killSwitch = false;
  let last: RiskState | null = null;
  /** `${day}|${mode}` -> latched disable */
  const disables = new Map<string, { until: number; reason: string }>();
  let overSince: { key: string; ts: number } | null = null;
  let entriesPaused = false;
  let manualClear = false;

  const notice = (level: "info" | "success" | "warn" | "error", title: string, message: string) =>
    ctx.emit({ type: "notice", data: { id: `risk-${ctx.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`, ts: ctx.now(), level, title, message } });

  const compute = (): { s: RiskState; newly: boolean } => {
    const st = ctx.settings();
    const limits = st.risk ?? DEFAULT_RISK_LIMITS;
    const mode = st.mode;
    const now = ctx.now();
    const day = etDay(now);
    const key = `${day}|${mode}`;
    const inputs = ctx.modules.trading.riskInputs(ctx.current().id);
    let d = disables.get(key) ?? null;
    if (d && d.until <= now) {
      disables.delete(key);
      d = null;
    }
    const loss = dailyLoss(inputs.dailyRealizedPnlUsd, inputs.unrealizedPnlUsd);
    const realizedLoss = Math.max(0, -inputs.dailyRealizedPnlUsd);
    let newly = false;
    entriesPaused = false;
    if (!d) {
      const realizedHit = atLossLimit(realizedLoss, limits.dailyLossLimitUsd);
      if (!realizedHit && atLossLimit(loss, limits.dailyLossLimitUsd)) {
        if (!overSince || overSince.key !== key) overSince = { key, ts: now };
      } else overSince = null;
      if (realizedHit || (overSince && now - overSince.ts >= DAILY_LOSS_CONFIRM_MS)) {
        overSince = null;
        const until = nextEtMidnight(now);
        d = { until, reason: `Daily loss limit reached (${usd(loss)} of ${usd(limits.dailyLossLimitUsd)}); trading resumes ${etTime(until)} ET` };
        disables.set(key, d);
        newly = true;
      } else if (overSince) entriesPaused = true;
    }
    const s = computeRiskState({
      limits,
      mode,
      now,
      ...inputs,
      killSwitch,
      tradingDisabledUntil: d?.until ?? null,
      disabledReason: d?.reason ?? null,
    });
    return { s, newly };
  };

  const refresh = (): RiskState => {
    const prev = last;
    const { s: next, newly } = compute();
    last = next;
    const cleared = manualClear;
    manualClear = false;
    if (newly) {
      notice("error", "Trading disabled", next.disabledReason ?? "Daily loss limit reached");
      const n = ctx.modules.trading.cancelRestingBuys?.(next.mode) ?? 0;
      if (n) notice("warn", "Resting buys cancelled", `${n} resting ${next.mode} buy order${n === 1 ? "" : "s"} cancelled (daily loss limit).`);
    }
    if (!cleared && prev && prev.tradingDisabledUntil != null && next.tradingDisabledUntil == null && prev.mode === next.mode) {
      notice("info", "Trading re-enabled", `New trading day (${next.day} ET): the daily loss limit was reset.`);
    }
    const changed = !prev || JSON.stringify({ ...prev, updatedTs: 0 }) !== JSON.stringify({ ...next, updatedTs: 0 });
    if (changed) ctx.emit({ type: "risk", data: next });
    return next;
  };

  const clearDisable = (mode: TradingMode) => {
    const s = refresh();
    if (s.tradingDisabledUntil == null) return;
    if (atLossLimit(s.dailyLossUsedUsd, s.limits.dailyLossLimitUsd)) {
      throw bad(
        `clearDisable: today's loss (${usd(s.dailyLossUsedUsd)}) is at the daily loss limit (${usd(s.limits.dailyLossLimitUsd)}); raise dailyLossLimitUsd above it first`,
      );
    }
    disables.delete(`${s.day}|${mode}`);
    manualClear = true;
    notice("warn", "Trading re-enabled (override)", "Today's daily-loss disable was lifted by hand.");
  };

  const self: MockRisk = {
    name: "risk",
    state: () => last ?? refresh(),
    check: (o) => {
      const s = refresh();
      if (!o.reducing && entriesPaused && !s.killSwitch && s.tradingDisabledUntil == null)
        return { ok: false, code: "trading_disabled", reason: "Daily loss limit reached (incl. unrealized); new entries paused" };
      return checkOrder(s, o, ctx.now());
    },
    refresh,
    kill() {
      killSwitch = true;
      refresh();
      const cancelledOrders = ctx.modules.trading.cancelAll("kill");
      const closedPositions = ctx.modules.trading.flattenAll("kill");
      const risk = refresh();
      const left = ctx.modules.trading.positions().filter((p) => p.status === "open" && p.windowId === ctx.current().id).length;
      notice(
        left ? "error" : "warn",
        left ? "Kill switch: action needed" : "Kill switch engaged",
        `Cancelled ${cancelledOrders} order${cancelledOrders === 1 ? "" : "s"}, closed ${closedPositions} position${closedPositions === 1 ? "" : "s"}${left ? `; ${left} still open (retrying every second)` : ""}. New entries are blocked until re-armed.`,
      );
      return { cancelledOrders, closedPositions, risk };
    },
    routes: [
      { method: "GET", path: "/risk", handler: () => self.state() },
      {
        method: "PUT",
        path: "/risk",
        handler: ({ body }) => {
          if (body == null || typeof body !== "object" || Array.isArray(body)) throw bad("body: must be a RiskPatch object");
          const raw = body as Record<string, unknown>;
          if (raw.killSwitch === true) throw bad("killSwitch: use POST /api/kill to engage the kill switch");
          const { killSwitch: ks, clearDisable: cd, ...rest } = body as RiskPatch;
          const limits: Partial<RiskLimits> = {};
          for (const [k, v] of Object.entries(rest)) {
            if (!(LIMIT_KEYS as readonly string[]).includes(k)) throw bad(`${k}: unknown field`);
            if (typeof v !== "number" || !Number.isFinite(v) || v < 0 || v > 1_000_000) throw bad(`${k}: must be a number between 0 and 1,000,000`);
            limits[k as keyof RiskLimits] = v;
          }
          if (limits.dailyLossLimitUsd !== undefined && !(limits.dailyLossLimitUsd > 0)) throw bad("dailyLossLimitUsd: must be greater than $0");
          if (Object.keys(limits).length) ctx.putSettings({ risk: limits });
          if (cd === true) clearDisable(ctx.settings().mode);
          if (ks === false && killSwitch) {
            killSwitch = false;
            notice("info", "Kill switch re-armed", "New entries are allowed again.");
          }
          return refresh();
        },
      },
      { method: "POST", path: "/kill", handler: () => self.kill() },
    ],
    onSecond: () => void refresh(),
    onSettings: () => void refresh(),
    snapshot: () => ({ risk: self.state() }),
  };
  return self;
}
