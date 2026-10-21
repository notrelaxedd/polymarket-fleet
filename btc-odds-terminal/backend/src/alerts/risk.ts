// Risk controls: limits, pre-trade check, daily loss auto-disable, kill switch. OWNER: alerts-risk.
// (backend/src/risk/index.ts re-exports createRisk from here.)
//
// Routes: GET /api/risk, PUT /api/risk (RiskPatch: limits persist via engine.updateSettings({ risk });
//         { killSwitch: false } re-arms the kill switch), POST /api/kill. Broadcasts `risk` on change, plus
//         `notice` toasts when trading is disabled / re-enabled and when the kill switch runs.
//
// Policy
//   - Limits (Settings.risk, default DEFAULT_RISK_LIMITS) are checked before every non-reducing order by
//     check() (trading calls it): max position cost, max open exposure (open cost + resting buys), max cost at
//     risk per window (open cost + resting buys + realized loss in the window; a binary contract's worst case
//     is its cost). Exits, closes, bracket legs and kill orders always pass. A limit of 0 blocks new entries.
//   - Daily loss = -(realized P&L today + unrealized P&L when negative), fees included, per trading mode.
//     Trading is DISABLED UNTIL THE NEXT America/New_York MIDNIGHT (00:00 ET, DST-aware: 04:00 UTC in EDT,
//     05:00 UTC in EST) when the REALIZED loss alone reaches dailyLossLimitUsd, or when the total loss
//     (realized + unrealized) stays at the limit for DAILY_LOSS_CONFIRM_MS (a one-tick thin bid cannot latch
//     it). While the total is at the limit but not yet confirmed, new entries are rejected (not latched).
//     A zero loss never trips; PUT /api/risk rejects dailyLossLimitUsd <= 0. Once latched the disable sticks
//     for the rest of that ET day even if the loss recovers or the limit is raised, unless the user lifts it
//     with the confirmed override PUT /api/risk { clearDisable: true } (refused while today's loss is still at
//     the limit: raise the limit first). Resting buy orders OF THAT MODE (pending included) are cancelled,
//     with retries; a cancel that keeps failing is reported in a notice. Open positions are left to their
//     brackets. The "day" of every daily counter is the ET calendar day (shared/risk.ts etDay()), the same
//     day the trading module books realized P&L under.
//   - Kill switch (POST /api/kill): engages first (blocks new entries at once, even when the risk state
//     cannot be computed), then cancels every resting order and flattens every open position of the current
//     window (trading.cancelAll / flattenAll with reason "kill"), then waits up to KILL_SETTLE_MAX_MS for
//     entries already in flight (pending buys) to resolve, flattening positions they open and cancelling
//     buys they leave resting. A failing step is reported in `problems`, never aborts the rest. It stays
//     engaged across days and restarts until re-armed with PUT /api/risk { killSwitch: false }. It is global
//     (both modes).
//   - Persistence: table risk_daily, one row per (ET day, mode): realized P&L, loss used, disabled-until /
//     reason, kill switch. The kill switch is read back from the most recently updated row.
//   - The state is recomputed every RISK_REFRESH_MS (marks move unrealized P&L; the ET day rolls over), on
//     settings changes, on every check() and whenever trading calls refresh() after a fill.

import type { KillResult, Notice, RiskState, Settings, TradingMode } from "../../../shared/protocol.ts";
import { DEFAULT_RISK_LIMITS } from "../../../shared/protocol.ts";
import { checkOrder, computeRiskState, DAILY_LOSS_CONFIRM_MS, etDay, nextEtMidnight, type RiskCheckResult, type RiskOrderCheck } from "../../../shared/risk.ts";
import type { BackendModule, ModuleDeps, RiskService } from "../modules/types.ts";
import { randomUUID } from "node:crypto";
import { badRequest } from "../server/errors.ts";
import { fmtUsd } from "./message.ts";
import { parseRiskPatch } from "./validate.ts";

/** Recompute cadence (marks, ET day rollover). */
export const RISK_REFRESH_MS = 1_000;
/** risk_daily rows are rewritten at most this often when only the P&L numbers moved (flags write at once). */
export const RISK_PERSIST_MS = 15_000;
/** Total (realized + unrealized) loss must stay at the daily limit this long before it latches (shared/risk.ts). */
export { DAILY_LOSS_CONFIRM_MS };
/** Cancel of resting buys after a daily-loss disable: attempts and the gap between them. */
export const RISK_CANCEL_ATTEMPTS = 3;
export const RISK_CANCEL_RETRY_MS = 1_000;
/** Kill switch: how long / how often it waits for in-flight entries after cancel + flatten. */
export const KILL_SETTLE_MAX_MS = 5_000;
export const KILL_SETTLE_POLL_MS = 200;

const sleep = (ms: number) =>
  new Promise<void>((r) => {
    const t = setTimeout(r, ms);
    t.unref?.();
  });

const WORKING = ["pending", "open", "partial"] as const;
const errMsg = (err: unknown) => (err instanceof Error ? err.message : String(err));

interface DailyRow {
  day: string;
  mode: string;
  realized_pnl_usd: number;
  loss_used_usd: number;
  disabled_until: number | null;
  disabled_reason: string | null;
  kill_switch: number;
  updated_ts: number;
}

interface Disable {
  until: number | null;
  reason: string | null;
}

const etTimeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

/** "Oct 21, 12:00 AM EDT" */
export const fmtEt = (ts: number) => etTimeFmt.format(new Date(ts));

/** today's loss is at the daily loss limit (a zero loss never is) */
const atDailyLimit = (s: RiskState) => s.dailyLossUsedUsd > 1e-9 && s.dailyLossUsedUsd >= s.limits.dailyLossLimitUsd - 1e-9;

const sameState = (a: RiskState, b: RiskState) =>
  JSON.stringify({ ...a, updatedTs: 0 }) === JSON.stringify({ ...b, updatedTs: 0 });

export interface RiskModule extends BackendModule<RiskService> {
  /** the risk_daily row of (day, mode), for tests / diagnostics */
  dailyRow(day: string, mode: TradingMode): DailyRow | null;
}

export function createRisk(deps: ModuleDeps): RiskModule {
  const db = deps.db.raw;
  const q = {
    get: db.prepare<[string, string], DailyRow>("SELECT * FROM risk_daily WHERE day = ? AND mode = ?"),
    latest: db.prepare<[], DailyRow>("SELECT * FROM risk_daily ORDER BY updated_ts DESC LIMIT 1"),
    upsert: db.prepare(
      `INSERT INTO risk_daily (day, mode, realized_pnl_usd, loss_used_usd, disabled_until, disabled_reason, kill_switch, updated_ts)
       VALUES (@day, @mode, @realized, @loss, @until, @reason, @kill, @ts)
       ON CONFLICT (day, mode) DO UPDATE SET realized_pnl_usd = excluded.realized_pnl_usd,
         loss_used_usd = excluded.loss_used_usd, disabled_until = excluded.disabled_until,
         disabled_reason = excluded.disabled_reason, kill_switch = excluded.kill_switch, updated_ts = excluded.updated_ts`,
    ),
  };

  let killSwitch = (q.latest.get()?.kill_switch ?? 0) === 1;
  /** `${day}|${mode}` -> auto-disable of that ET day (loaded lazily from risk_daily) */
  const disables = new Map<string, Disable>();
  let last: RiskState | null = null;
  let lastPersist: { key: string; ts: number; flags: string; numbers: string } | null = null;
  let killing: Promise<KillResult> | null = null;
  let timer: NodeJS.Timeout | null = null;
  /** total loss at the limit (unconfirmed) since ts, for `${day}|${mode}` */
  let overSince: { key: string; ts: number } | null = null;
  /** set by compute(): total loss is at the limit but not confirmed yet -> new entries rejected */
  let entriesPaused = false;
  /** the next disabled -> enabled transition is the manual override (not a new trading day) */
  let manualClear = false;

  const disableFor = (day: string, mode: TradingMode): Disable => {
    const key = `${day}|${mode}`;
    let d = disables.get(key);
    if (!d) {
      const row = q.get.get(day, mode);
      d = { until: row?.disabled_until ?? null, reason: row?.disabled_reason ?? null };
      disables.set(key, d);
      // keep the cache to today (+ the other mode)
      if (disables.size > 8) for (const k of disables.keys()) if (!k.startsWith(`${day}|`)) disables.delete(k);
    }
    return d;
  };

  const notice = (level: Notice["level"], title: string, message: string) =>
    deps.broadcast({ type: "notice", data: { id: randomUUID(), ts: deps.now(), level, title, message } });

  const persist = (s: RiskState, force = false) => {
    const key = `${s.day}|${s.mode}`;
    const flags = JSON.stringify([s.tradingDisabledUntil, s.disabledReason, s.killSwitch]);
    const numbers = JSON.stringify([s.dailyRealizedPnlUsd, s.dailyLossUsedUsd]);
    const p = lastPersist;
    const due =
      force || !p || p.key !== key || p.flags !== flags || (p.numbers !== numbers && s.updatedTs - p.ts >= RISK_PERSIST_MS);
    if (!due) return;
    try {
      q.upsert.run({
        day: s.day,
        mode: s.mode,
        realized: s.dailyRealizedPnlUsd,
        loss: s.dailyLossUsedUsd,
        until: s.tradingDisabledUntil,
        reason: s.disabledReason,
        kill: s.killSwitch ? 1 : 0,
        ts: s.updatedTs,
      });
      lastPersist = { key, ts: s.updatedTs, flags, numbers };
    } catch (err) {
      deps.logger.error({ err }, "risk_daily write failed");
    }
  };

  /** working buy orders (pending / open / partial) of `mode`, or of every mode when null */
  const workingBuys = (mode: TradingMode | null) => {
    const trading = deps.services.trading;
    return WORKING.flatMap((status) => trading.orders({ status, limit: 1000 })).filter(
      (o) => o.action === "buy" && (mode == null || o.mode === mode),
    );
  };

  /**
   * Resting entries add exposure when they fill: cancel the disabled mode's working buys (pending included),
   * retrying what failed; whatever still cannot be cancelled is reported in a notice.
   */
  const cancelRestingBuys = async (mode: TradingMode, why: string) => {
    const trading = deps.services.trading;
    let n = 0;
    let failed: string[] = [];
    for (let attempt = 0; attempt < RISK_CANCEL_ATTEMPTS; attempt++) {
      if (attempt) await sleep(RISK_CANCEL_RETRY_MS);
      failed = [];
      for (const o of workingBuys(mode)) {
        try {
          await trading.cancelOrder(o.id);
          n++;
        } catch (err) {
          failed.push(o.id);
          deps.logger.warn({ err: errMsg(err), orderId: o.id, attempt }, "risk: cancel of resting buy failed");
        }
      }
      if (!failed.length) break;
    }
    if (n) deps.logger.warn({ cancelled: n, why, mode }, "risk: resting buys cancelled");
    if (failed.length) {
      notice(
        "error",
        "Trading disabled: cancel failed",
        `${failed.length} resting ${mode} buy order${failed.length === 1 ? "" : "s"} could not be cancelled and may still fill. Cancel ${failed.length === 1 ? "it" : "them"} by hand.`,
      );
    }
  };

  const compute = (): RiskState => {
    const now = deps.now();
    const settings: Settings = deps.engine.getSettings();
    const limits = settings.risk ?? DEFAULT_RISK_LIMITS;
    const mode = settings.mode;
    const day = etDay(now);
    const inputs = deps.services.trading.riskInputs(deps.engine.currentWindow()?.id ?? null);
    const d = disableFor(day, mode);
    if (d.until != null && d.until <= now) {
      d.until = null;
      d.reason = null;
    }
    const loss = Math.max(0, -(inputs.dailyRealizedPnlUsd + Math.min(0, inputs.unrealizedPnlUsd)));
    const realizedLoss = Math.max(0, -inputs.dailyRealizedPnlUsd);
    // a zero loss never trips (a limit of 0 means "no loss at all", not "disabled")
    const atLimit = (x: number) => x > 1e-9 && x >= limits.dailyLossLimitUsd - 1e-9;
    let newlyDisabled = false;
    entriesPaused = false;
    if (d.until == null) {
      const key = `${day}|${mode}`;
      const realizedHit = atLimit(realizedLoss);
      // unrealized loss (marked at the best bid) must persist before it latches: a one-tick thin bid cannot
      if (!realizedHit && atLimit(loss)) {
        if (!overSince || overSince.key !== key) overSince = { key, ts: now };
      } else {
        overSince = null;
      }
      if (realizedHit || (overSince && now - overSince.ts >= DAILY_LOSS_CONFIRM_MS)) {
        overSince = null;
        d.until = nextEtMidnight(now);
        d.reason = `Daily loss limit reached (${fmtUsd(loss)} of ${fmtUsd(limits.dailyLossLimitUsd)}); trading resumes ${fmtEt(d.until)}`;
        newlyDisabled = true;
      } else if (overSince) {
        entriesPaused = true;
      }
    }
    // the policy above decides the disable (computeRiskState's own baseline latch is overridden)
    const s: RiskState = {
      ...computeRiskState({
        limits,
        mode,
        now,
        ...inputs,
        killSwitch,
        tradingDisabledUntil: d.until,
        disabledReason: d.reason,
      }),
      tradingDisabledUntil: d.until,
      disabledReason: d.until != null ? d.reason : null,
    };
    if (newlyDisabled) {
      deps.logger.warn({ day, mode, loss, limit: limits.dailyLossLimitUsd, until: d.until }, "risk: daily loss limit hit, trading disabled");
      notice("error", "Trading disabled", d.reason ?? "Daily loss limit reached");
      persist(s, true);
      setImmediate(() => void cancelRestingBuys(mode, "daily loss limit").catch((err) => deps.logger.error({ err }, "risk: cancel failed")));
    }
    return s;
  };

  const refresh = (): RiskState => {
    const prev = last;
    const next = compute();
    last = next;
    const cleared = manualClear;
    manualClear = false;
    if (!cleared && prev && prev.tradingDisabledUntil != null && next.tradingDisabledUntil == null && prev.mode === next.mode) {
      notice("info", "Trading re-enabled", `New trading day (${next.day} ET): the daily loss limit was reset.`);
    }
    persist(next);
    if (!prev || !sameState(prev, next)) deps.broadcast({ type: "risk", data: next });
    return next;
  };

  /** refresh() that never throws; null when the inputs are unavailable. */
  const safeRefresh = (): RiskState | null => {
    try {
      return refresh();
    } catch (err) {
      deps.logger.error({ err }, "risk refresh failed");
      return null;
    }
  };

  const setKillSwitch = (on: boolean) => {
    killSwitch = on;
    const s = refresh();
    persist(s, true);
    return s;
  };

  /** The last known state with the current kill flag (when a refresh cannot be computed). */
  const fallbackState = (): RiskState => {
    const now = deps.now();
    if (last) return { ...last, killSwitch, updatedTs: now };
    let limits = DEFAULT_RISK_LIMITS;
    let mode: TradingMode = "paper";
    try {
      const st = deps.engine.getSettings();
      limits = st.risk ?? limits;
      mode = st.mode;
    } catch {
      // defaults
    }
    const zero = { dailyRealizedPnlUsd: 0, unrealizedPnlUsd: 0, openExposureUsd: 0, windowLossUsedUsd: 0 };
    const base = computeRiskState({ limits, mode, now, ...zero, killSwitch, tradingDisabledUntil: null, disabledReason: null });
    return { ...base, tradingDisabledUntil: null, disabledReason: null };
  };

  /** Manual override of today's daily-loss disable (PUT /api/risk { clearDisable: true }). */
  const clearDisable = (): void => {
    const s = refresh();
    if (s.tradingDisabledUntil == null) return;
    if (atDailyLimit(s)) {
      throw badRequest(
        `clearDisable: today's loss (${fmtUsd(s.dailyLossUsedUsd)}) is at the daily loss limit (${fmtUsd(s.limits.dailyLossLimitUsd)}); raise dailyLossLimitUsd above it first`,
      );
    }
    const d = disableFor(s.day, s.mode);
    d.until = null;
    d.reason = null;
    overSince = null;
    manualClear = true;
    deps.logger.warn({ day: s.day, mode: s.mode, loss: s.dailyLossUsedUsd, limit: s.limits.dailyLossLimitUsd }, "risk: daily loss disable cleared by override");
    persist(refresh(), true);
    notice(
      "warn",
      "Trading re-enabled (override)",
      `Daily loss disable lifted for ${s.day} ET (${s.mode}). Trading disables again if today's loss reaches ${fmtUsd(s.limits.dailyLossLimitUsd)}.`,
    );
  };

  const check = (o: RiskOrderCheck): RiskCheckResult => {
    if (o.reducing) return { ok: true };
    const nums = [o.addCostUsd, o.windowCostUsd, o.positionCostUsd];
    if (!nums.every((n) => Number.isFinite(n) && n >= 0)) {
      return { ok: false, code: "risk_rejected", reason: "Order cost could not be determined" };
    }
    const s = safeRefresh();
    // fail closed: no new risk while the risk state cannot be computed
    if (!s) return { ok: false, code: "trading_disabled", reason: "Risk state unavailable" };
    const r: RiskCheckResult =
      entriesPaused && !s.killSwitch && s.tradingDisabledUntil == null
        ? { ok: false, code: "trading_disabled", reason: "Daily loss limit reached (incl. unrealized); new entries paused" }
        : checkOrder(s, o, deps.now());
    if (!r.ok) deps.logger.info({ ...o, reason: r.reason }, "risk: order rejected");
    return r;
  };

  /**
   * Entries that passed check() before the switch engaged may still be submitting: wait (bounded) for the
   * current window's pending buys to resolve; flatten positions they open and cancel buys they leave resting.
   */
  const settleKill = async (problems: string[]) => {
    const trading = deps.services.trading;
    let cancelled = 0;
    let closed = 0;
    const openHere = () => {
      const id = deps.engine.currentWindow()?.id ?? null;
      return id ? trading.positions({ status: "open", limit: 1000 }).filter((p) => p.windowId === id) : [];
    };
    const handled = new Set(openHere().map((p) => p.id)); // flattenAll already ran for these
    const cancelTried = new Set<string>();
    const polls = Math.max(1, Math.ceil(KILL_SETTLE_MAX_MS / KILL_SETTLE_POLL_MS));
    let inFlight = 0;
    for (let i = 0; i < polls; i++) {
      const curId = deps.engine.currentWindow()?.id ?? null;
      const buys = workingBuys(null);
      for (const o of buys) {
        if (o.status === "pending" || cancelTried.has(o.id)) continue;
        cancelTried.add(o.id);
        try {
          await trading.cancelOrder(o.id);
          cancelled++;
        } catch (err) {
          problems.push(`cancel of ${o.id} failed: ${errMsg(err)}`);
        }
      }
      const fresh = openHere().filter((p) => !handled.has(p.id));
      if (fresh.length) {
        try {
          closed += await trading.flattenAll("kill");
        } catch (err) {
          problems.push(`flatten failed: ${errMsg(err)}`);
        }
        for (const p of openHere()) handled.add(p.id);
        continue;
      }
      inFlight = buys.filter((o) => o.status === "pending" && curId != null && o.windowId === curId).length;
      if (!inFlight) break;
      await sleep(KILL_SETTLE_POLL_MS);
    }
    if (inFlight) problems.push(`${inFlight} entry order${inFlight === 1 ? "" : "s"} still in flight`);
    return { cancelled, closed };
  };

  const kill = (): Promise<KillResult> => {
    if (killing) return killing;
    killing = (async (): Promise<KillResult> => {
      deps.logger.warn("risk: KILL SWITCH engaged");
      const problems: string[] = [];
      // engage first: check() rejects entries from here on (and fails closed if the state cannot be computed)
      try {
        setKillSwitch(true);
      } catch (err) {
        killSwitch = true;
        problems.push(`risk state unavailable: ${errMsg(err)}`);
        if (last) persist(fallbackState(), true);
      }
      const trading = deps.services.trading;
      let cancelledOrders = 0;
      let closedPositions = 0;
      try {
        cancelledOrders = await trading.cancelAll("kill");
      } catch (err) {
        problems.push(`cancel failed: ${errMsg(err)}`);
      }
      // live backstop: the exchange's own cancel-all for the markets we trade (catches orders trading never tracked)
      try {
        const ex = deps.services.live?.executor();
        if (ex?.cancelAllRemote) {
          const slugs = [deps.engine.currentWindow()?.id, deps.engine.nextWindow()?.id].filter((x): x is string => !!x);
          if (slugs.length) await ex.cancelAllRemote(slugs);
        }
      } catch (err) {
        problems.push(`exchange cancel-all failed: ${errMsg(err)}`);
      }
      try {
        closedPositions = await trading.flattenAll("kill");
      } catch (err) {
        problems.push(`flatten failed: ${errMsg(err)}`);
      }
      try {
        const r = await settleKill(problems);
        cancelledOrders += r.cancelled;
        closedPositions += r.closed;
      } catch (err) {
        problems.push(`in-flight check failed: ${errMsg(err)}`);
      }
      try {
        const cur = deps.engine.currentWindow();
        const left = trading.positions({ status: "open", limit: 1000 }).filter((p) => cur && p.windowId === cur.id).length;
        if (left) problems.push(`${left} position${left === 1 ? "" : "s"} still open`);
      } catch {
        // listing is best effort
      }
      let risk = safeRefresh();
      if (!risk) {
        problems.push("risk state could not be refreshed");
        risk = fallbackState();
      }
      const summary = `Cancelled ${cancelledOrders} order${cancelledOrders === 1 ? "" : "s"}, closed ${closedPositions} position${closedPositions === 1 ? "" : "s"}. Trading stays off until re-armed.`;
      if (problems.length) {
        deps.logger.error({ problems, cancelledOrders, closedPositions }, "risk: kill switch incomplete");
        notice("error", "Kill switch: action needed", `${summary} ${problems.join("; ")}.`);
      } else {
        deps.logger.warn({ cancelledOrders, closedPositions }, "risk: kill switch done");
        notice("warn", "Kill switch engaged", summary);
      }
      return { cancelledOrders, closedPositions, risk };
    })().finally(() => {
      killing = null;
    });
    return killing;
  };

  const service: RiskService = {
    state: () => last ?? refresh(),
    check,
    refresh,
    kill,
  };

  const onSettings = () => void safeRefresh();

  return {
    name: "risk",
    service,
    dailyRow: (day, mode) => q.get.get(day, mode) ?? null,
    register(app) {
      app.get("/api/risk", async () => refresh());
      app.put("/api/risk", async (req) => {
        const { limits, rearm, clearDisable: clear } = parseRiskPatch(req.body);
        if (Object.keys(limits).length) deps.engine.updateSettings({ risk: limits });
        if (clear) clearDisable();
        if (rearm) {
          if (killing) await killing.catch(() => undefined);
          if (killSwitch) {
            deps.logger.warn("risk: kill switch re-armed");
            setKillSwitch(false);
            notice("info", "Kill switch re-armed", "New orders are allowed again (risk limits still apply).");
          }
        }
        return refresh();
      });
      app.post("/api/kill", async () => kill());
    },
    start() {
      deps.engine.on("settings", onSettings);
      safeRefresh();
      timer = setInterval(() => void safeRefresh(), RISK_REFRESH_MS);
      timer.unref?.();
    },
    stop() {
      deps.engine.off("settings", onSettings);
      if (timer) clearInterval(timer);
      timer = null;
    },
    snapshot: () => ({ risk: service.state() }),
  };
}
