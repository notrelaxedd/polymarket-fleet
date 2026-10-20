// Risk controls (backend/src/alerts/risk.ts via backend/src/risk): pre-trade limits, the daily loss limit that
// disables trading until the next America/New_York midnight, persistence in risk_daily, the kill switch
// (cancel all + flatten) and the /api/risk + /api/kill routes.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MarketWindow, Notice, Order, RiskLimits, RiskState, ServerMsg, Settings, StateSnapshot } from "../../shared/protocol.ts";
import { DEFAULT_RISK_LIMITS } from "../../shared/protocol.ts";
import { etDay, nextEtMidnight } from "../../shared/risk.ts";
import type { Config } from "../src/config.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import type { EngineApi, ModuleDeps, Services, TradingService } from "../src/modules/types.ts";
import { createRisk } from "../src/risk/index.ts";
import { DAILY_LOSS_CONFIRM_MS, KILL_SETTLE_POLL_MS, RISK_CANCEL_RETRY_MS } from "../src/alerts/risk.ts";
import { buildServer } from "../src/server/index.ts";

const T0 = Date.parse("2026-10-20T18:00:00Z"); // 14:00 EDT

class FakeEngine extends EventEmitter {
  settings: Settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "", risk: { ...DEFAULT_RISK_LIMITS } };
  window: MarketWindow | null = {
    id: "w1",
    title: "w1",
    startTs: T0 - 60_000,
    endTs: T0 + 840_000,
    priceToBeat: 67_000,
    upInstrument: "m:long",
    downInstrument: "m:short",
    status: "live",
    result: null,
    settlePrice: null,
  };
  getSettings = () => ({ ...this.settings, risk: { ...(this.settings.risk as RiskLimits) } });
  updateSettings = vi.fn((patch: unknown) => {
    const p = patch as { risk?: Partial<RiskLimits> };
    if (p.risk) this.settings.risk = { ...(this.settings.risk as RiskLimits), ...p.risk };
    const s = this.getSettings();
    this.emit("settings", s);
    return s;
  });
  currentWindow = () => this.window;
  nextWindow = () => null;
  latestTick = () => null;
  latestOdds = () => null;
  latestFair = () => null;
  currentBook = () => null;
  refreshBook = async () => null;
  priceAt = () => null;
  liveAvailable = () => false;
  feeds = () => [];
  snapshot = (): StateSnapshot => ({
    serverTime: 0,
    settings: this.getSettings(),
    current: null,
    next: null,
    recent: [],
    lastTick: null,
    lastOdds: null,
    lastFair: null,
    feeds: [],
  });
}

const order = (id: string, action: "buy" | "sell", status: Order["status"], mode: Order["mode"] = "paper"): Order =>
  ({ id, action, status, mode, windowId: "w1" }) as Order;

function fakeTrading() {
  const inputs = { dailyRealizedPnlUsd: 0, unrealizedPnlUsd: 0, openExposureUsd: 0, windowLossUsedUsd: 0 };
  const working: Order[] = [];
  const t = {
    inputs,
    working,
    riskInputs: vi.fn(() => ({ ...inputs })),
    cancelAll: vi.fn(async () => 2),
    flattenAll: vi.fn(async () => 1),
    cancelOrder: vi.fn(async (id: string) => ({ ...working.find((o) => o.id === id)!, status: "cancelled" as const })),
    orders: vi.fn((q?: { status?: string }) => working.filter((o) => !q?.status || o.status === q.status)),
    positions: vi.fn(() => []),
  };
  return t;
}

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setup(opts: { db?: TerminalDb; now?: number } = {}) {
  const db = opts.db ?? openDb(":memory:");
  const engine = new FakeEngine();
  const trading = fakeTrading();
  const msgs: ServerMsg[] = [];
  const clock = { now: opts.now ?? T0 };
  const deps: ModuleDeps = {
    config: {} as Config,
    db,
    engine: engine as unknown as EngineApi,
    broadcast: (m) => msgs.push(m),
    logger: pino({ level: "silent" }),
    now: () => clock.now,
    services: { trading: trading as unknown as TradingService } as Services,
  };
  const mod = createRisk(deps);
  const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => mod.register(a), feedsIntervalMs: 60_000 });
  mod.start?.();
  cleanup.push(async () => {
    await app.close();
    await mod.stop?.();
  });
  const notices = () => msgs.filter((m): m is { type: "notice"; data: Notice } => m.type === "notice").map((m) => m.data);
  const risks = () => msgs.filter((m): m is { type: "risk"; data: RiskState } => m.type === "risk").map((m) => m.data);
  return { db, engine, trading, msgs, clock, mod, app, notices, risks, svc: mod.service, deps };
}

const entry = (addCostUsd: number, windowCostUsd = 0, positionCostUsd = 0) => ({ addCostUsd, windowCostUsd, positionCostUsd, reducing: false });

describe("risk: pre-trade check", () => {
  it("enforces max position, max open exposure and max loss per window; exits always pass", () => {
    const { svc, trading, engine } = setup();
    engine.settings.risk = { dailyLossLimitUsd: 200, maxExposureUsd: 300, maxLossPerWindowUsd: 100, maxPositionUsd: 80 };
    expect(svc.check(entry(50))).toEqual({ ok: true });
    expect(svc.check(entry(50, 0, 40))).toMatchObject({ ok: false, code: "risk_rejected", reason: "Max position $80" });
    expect(svc.check(entry(80, 0, 0))).toEqual({ ok: true }); // exactly at the limit
    expect(svc.check(entry(30, 80, 0))).toMatchObject({ ok: false, code: "risk_rejected", reason: "Max loss per window $100" });
    trading.inputs.openExposureUsd = 280;
    expect(svc.check(entry(30))).toMatchObject({ ok: false, code: "risk_rejected", reason: "Max open exposure $300" });
    expect(svc.check({ ...entry(1000, 1000, 1000), reducing: true })).toEqual({ ok: true });
  });

  it("rejects orders whose cost is not a finite non-negative number", () => {
    const { svc } = setup();
    expect(svc.check(entry(Number.NaN))).toMatchObject({ ok: false, code: "risk_rejected" });
    expect(svc.check(entry(-5))).toMatchObject({ ok: false, code: "risk_rejected" });
  });

  it("fails closed when the risk inputs cannot be computed", () => {
    const { svc, trading } = setup();
    trading.riskInputs.mockImplementation(() => {
      throw new Error("db gone");
    });
    expect(svc.check(entry(1))).toMatchObject({ ok: false, code: "trading_disabled", reason: "Risk state unavailable" });
    expect(svc.check({ ...entry(1), reducing: true })).toEqual({ ok: true });
  });

  it("reports state from the trading inputs and broadcasts risk only when it changes", () => {
    const { svc, trading, risks } = setup();
    const n0 = risks().length;
    svc.refresh();
    svc.refresh();
    expect(risks().length).toBe(n0);
    trading.inputs.openExposureUsd = 42;
    trading.inputs.windowLossUsedUsd = 12;
    trading.inputs.dailyRealizedPnlUsd = -20;
    trading.inputs.unrealizedPnlUsd = 5; // positive unrealized does not offset realized loss
    const s = svc.refresh();
    expect(s).toMatchObject({ openExposureUsd: 42, windowLossUsedUsd: 12, dailyRealizedPnlUsd: -20, dailyLossUsedUsd: 20, day: "2026-10-20", mode: "paper" });
    expect(risks().length).toBe(n0 + 1);
    expect(trading.riskInputs).toHaveBeenLastCalledWith("w1");
  });
});

describe("risk: daily loss limit (America/New_York day)", () => {
  it("disables trading until the next ET midnight, cancels resting buys and sticks for the rest of the day", async () => {
    const { svc, trading, engine, clock, notices, mod } = setup();
    trading.working.push(
      order("b1", "buy", "open"),
      order("s1", "sell", "open"),
      order("b2", "buy", "partial"),
      order("b3", "buy", "pending"), // mid-submit: cancelled too
      order("L1", "buy", "open", "live"), // the other mode's orders are not touched
    );
    trading.inputs.dailyRealizedPnlUsd = -150;
    trading.inputs.unrealizedPnlUsd = -60; // 210 >= 200
    // unrealized loss must stay at the limit before it latches; meanwhile new entries are paused
    const s0 = svc.refresh();
    expect(s0.tradingDisabledUntil).toBeNull();
    expect(svc.check(entry(1))).toMatchObject({ ok: false, code: "trading_disabled", reason: expect.stringMatching(/entries paused/) });
    clock.now += DAILY_LOSS_CONFIRM_MS;
    const s = svc.refresh();
    expect(s.tradingDisabledUntil).toBe(Date.parse("2026-10-21T04:00:00Z")); // 00:00 EDT
    expect(s.disabledReason).toMatch(/^Daily loss limit reached \(\$210\.00 of \$200\.00\); trading resumes Oct 21, 12:00 AM EDT$/);
    expect(s.dailyLossUsedUsd).toBe(210);
    expect(notices().at(-1)).toMatchObject({ level: "error", title: "Trading disabled" });
    expect(svc.check(entry(1))).toMatchObject({ ok: false, code: "trading_disabled" });
    expect(svc.check({ ...entry(1), reducing: true })).toEqual({ ok: true });
    expect(mod.dailyRow("2026-10-20", "paper")).toMatchObject({ disabled_until: s.tradingDisabledUntil, loss_used_usd: 210, realized_pnl_usd: -150 });

    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    expect(trading.cancelOrder.mock.calls.map((c) => c[0]).sort()).toEqual(["b1", "b2", "b3"]);

    // the loss recovers and the limit is raised: still disabled for the rest of the ET day
    trading.inputs.dailyRealizedPnlUsd = -10;
    trading.inputs.unrealizedPnlUsd = 50;
    engine.updateSettings({ risk: { dailyLossLimitUsd: 1000 } });
    clock.now = Date.parse("2026-10-21T03:59:59Z"); // 23:59:59 EDT
    expect(svc.refresh().tradingDisabledUntil).toBe(Date.parse("2026-10-21T04:00:00Z"));
    expect(notices().filter((n) => n.title === "Trading disabled")).toHaveLength(1);

    // next ET day: re-enabled (the trading module books realized P&L per ET day, so it restarts at 0)
    clock.now = Date.parse("2026-10-21T04:00:00Z");
    trading.inputs.dailyRealizedPnlUsd = 0;
    trading.inputs.unrealizedPnlUsd = 0;
    const s2 = svc.refresh();
    expect(s2).toMatchObject({ day: "2026-10-21", tradingDisabledUntil: null, disabledReason: null });
    expect(notices().at(-1)).toMatchObject({ level: "info", title: "Trading re-enabled" });
    expect(svc.check(entry(1))).toEqual({ ok: true });
  });

  it("a one-tick unrealized dip does not latch; realized loss latches at once; zero loss never trips", () => {
    const { svc, trading, clock, engine, mod } = setup();
    trading.inputs.unrealizedPnlUsd = -250; // thin bid for one refresh
    expect(svc.refresh().tradingDisabledUntil).toBeNull();
    clock.now += 1000;
    trading.inputs.unrealizedPnlUsd = -20; // mark recovered
    expect(svc.refresh().tradingDisabledUntil).toBeNull();
    expect(svc.check(entry(1))).toEqual({ ok: true });
    clock.now += DAILY_LOSS_CONFIRM_MS; // the dip does not count from its old start
    trading.inputs.unrealizedPnlUsd = -250;
    expect(svc.refresh().tradingDisabledUntil).toBeNull();
    expect(mod.dailyRow("2026-10-20", "paper")?.disabled_until ?? null).toBeNull();

    trading.inputs.unrealizedPnlUsd = 0;
    engine.settings.risk = { ...DEFAULT_RISK_LIMITS, dailyLossLimitUsd: 0 };
    expect(svc.refresh().tradingDisabledUntil).toBeNull(); // 0 loss vs a 0 limit
    expect(svc.check(entry(1))).toEqual({ ok: true });

    engine.settings.risk = { ...DEFAULT_RISK_LIMITS };
    trading.inputs.dailyRealizedPnlUsd = -200;
    expect(svc.refresh().tradingDisabledUntil).toBe(Date.parse("2026-10-21T04:00:00Z"));
  });

  it("PUT /api/risk rejects a daily loss limit <= 0 and clearDisable lifts the disable once the limit is above the loss", async () => {
    const { app, svc, trading, engine, notices, mod } = setup();
    for (const v of [0, -5]) {
      const r = await app.inject({ method: "PUT", url: "/api/risk", payload: { dailyLossLimitUsd: v } });
      expect(r.statusCode).toBe(400);
      expect(r.json()).toMatchObject({ code: "validation", error: expect.stringMatching(/dailyLossLimitUsd/) });
    }
    expect(engine.updateSettings).not.toHaveBeenCalled();
    expect((await app.inject({ method: "PUT", url: "/api/risk", payload: { clearDisable: false } })).statusCode).toBe(400);

    trading.inputs.dailyRealizedPnlUsd = -250;
    expect(svc.refresh().tradingDisabledUntil).not.toBeNull();
    // still at the limit: refused
    const r1 = await app.inject({ method: "PUT", url: "/api/risk", payload: { clearDisable: true } });
    expect(r1.statusCode).toBe(400);
    expect(r1.json().error).toMatch(/raise dailyLossLimitUsd/);
    expect(svc.state().tradingDisabledUntil).not.toBeNull();

    // raise the limit and clear in one PUT
    const r2 = await app.inject({ method: "PUT", url: "/api/risk", payload: { dailyLossLimitUsd: 400, clearDisable: true } });
    expect(r2.statusCode, r2.body).toBe(200);
    expect(r2.json()).toMatchObject({ tradingDisabledUntil: null, disabledReason: null, limits: { dailyLossLimitUsd: 400 } });
    expect(notices().at(-1)).toMatchObject({ level: "warn", title: "Trading re-enabled (override)" });
    expect(notices().some((n) => n.title === "Trading re-enabled")).toBe(false);
    expect(mod.dailyRow("2026-10-20", "paper")).toMatchObject({ disabled_until: null });
    expect(svc.check(entry(1))).toEqual({ ok: true });

    // the new limit still applies
    trading.inputs.dailyRealizedPnlUsd = -400;
    expect(svc.refresh().tradingDisabledUntil).not.toBeNull();
  });

  it("retries a failed cancel of resting buys and reports it when it keeps failing", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "setImmediate"] });
    try {
      const { svc, trading, notices } = setup();
      trading.working.push(order("b1", "buy", "open"), order("b2", "buy", "pending"));
      trading.cancelOrder.mockImplementation(async (id: string) => {
        if (id === "b2") throw new Error("in flight");
        return { ...trading.working.find((o) => o.id === id)!, status: "cancelled" as const };
      });
      trading.inputs.dailyRealizedPnlUsd = -300;
      svc.refresh();
      await vi.advanceTimersByTimeAsync(RISK_CANCEL_RETRY_MS * 3);
      expect(trading.cancelOrder.mock.calls.filter((c) => c[0] === "b2")).toHaveLength(3);
      expect(notices().at(-1)).toMatchObject({ level: "error", title: "Trading disabled: cancel failed" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses the ET calendar day across EST and the DST change", () => {
    // 23:59:59 EST on Nov 30 is still Nov 30 in New York
    expect(etDay(Date.parse("2026-12-01T04:59:59Z"))).toBe("2026-11-30");
    expect(nextEtMidnight(Date.parse("2026-12-01T04:59:59Z"))).toBe(Date.parse("2026-12-01T05:00:00Z"));
    // Nov 1 2026 is the 25-hour fall-back day: midnight after it is 05:00 UTC
    expect(nextEtMidnight(Date.parse("2026-11-01T12:00:00Z"))).toBe(Date.parse("2026-11-02T05:00:00Z"));
    // Mar 8 2026 is the 23-hour spring-forward day
    expect(nextEtMidnight(Date.parse("2026-03-08T12:00:00Z"))).toBe(Date.parse("2026-03-09T04:00:00Z"));

    const { svc, trading } = setup({ now: Date.parse("2026-12-01T04:30:00Z") });
    trading.inputs.dailyRealizedPnlUsd = -250;
    const s = svc.refresh();
    expect(s.day).toBe("2026-11-30");
    expect(s.tradingDisabledUntil).toBe(Date.parse("2026-12-01T05:00:00Z"));
    expect(s.disabledReason).toContain("Dec 1, 12:00 AM EST");
  });

  it("keeps the counters per trading mode", () => {
    const { svc, trading, engine } = setup();
    trading.inputs.dailyRealizedPnlUsd = -300;
    expect(svc.refresh().tradingDisabledUntil).not.toBeNull();
    // switching to live: live has its own (clean) day
    trading.inputs.dailyRealizedPnlUsd = 0;
    engine.settings.mode = "live";
    expect(svc.refresh()).toMatchObject({ mode: "live", tradingDisabledUntil: null });
    engine.settings.mode = "paper";
    expect(svc.refresh()).toMatchObject({ mode: "paper", tradingDisabledUntil: expect.any(Number) });
  });

  it("persists the disable and the kill switch in risk_daily across restarts", async () => {
    const a = setup();
    a.trading.inputs.dailyRealizedPnlUsd = -500;
    a.svc.refresh();
    await a.svc.kill();
    await a.mod.stop?.();

    const b = setup({ db: a.db, now: T0 + 3_600_000 });
    const s = b.svc.state();
    expect(s.killSwitch).toBe(true);
    expect(s.tradingDisabledUntil).toBe(Date.parse("2026-10-21T04:00:00Z"));
    expect(s.disabledReason).toMatch(/Daily loss limit reached/);

    // the kill switch carries over to the next day too (until re-armed)
    const c = setup({ db: a.db, now: Date.parse("2026-10-22T15:00:00Z") });
    expect(c.svc.state()).toMatchObject({ killSwitch: true, tradingDisabledUntil: null, day: "2026-10-22" });
    const r = await c.app.inject({ method: "PUT", url: "/api/risk", payload: { killSwitch: false } });
    expect(r.json().killSwitch).toBe(false);
    const d = setup({ db: a.db, now: Date.parse("2026-10-22T16:00:00Z") });
    expect(d.svc.state().killSwitch).toBe(false);
  });
});

describe("risk: kill switch and routes", () => {
  it("POST /api/kill engages first, cancels all + flattens, and blocks entries until re-armed", async () => {
    const { app, svc, trading, notices } = setup();
    let engagedDuringCancel: boolean | null = null;
    trading.cancelAll.mockImplementation(async () => {
      engagedDuringCancel = svc.state().killSwitch;
      return 2;
    });
    const r = await app.inject({ method: "POST", url: "/api/kill" });
    expect(r.statusCode).toBe(200);
    expect(r.json()).toMatchObject({ cancelledOrders: 2, closedPositions: 1, risk: { killSwitch: true } });
    expect(engagedDuringCancel).toBe(true);
    expect(trading.cancelAll).toHaveBeenCalledWith("kill");
    expect(trading.flattenAll).toHaveBeenCalledWith("kill");
    expect(notices().at(-1)).toMatchObject({ level: "warn", title: "Kill switch engaged" });
    expect(svc.check(entry(1))).toMatchObject({ ok: false, code: "trading_disabled", reason: "Kill switch engaged" });
    expect(svc.check({ ...entry(1), reducing: true })).toEqual({ ok: true });

    const bad = await app.inject({ method: "PUT", url: "/api/risk", payload: { killSwitch: true } });
    expect(bad.statusCode).toBe(400);
    expect(bad.json()).toMatchObject({ code: "validation" });
    expect(bad.json().error).toMatch(/POST \/api\/kill/);

    const re = await app.inject({ method: "PUT", url: "/api/risk", payload: { killSwitch: false } });
    expect(re.json().killSwitch).toBe(false);
    expect(notices().at(-1)).toMatchObject({ title: "Kill switch re-armed" });
    expect(svc.check(entry(1))).toEqual({ ok: true });
  });

  it("concurrent kills share one run; a failing cancel still flattens and is reported", async () => {
    const { svc, trading, notices } = setup();
    trading.cancelAll.mockRejectedValue(new Error("exchange down"));
    const [a, b] = await Promise.all([svc.kill(), svc.kill()]);
    expect(a).toBe(b);
    expect(trading.cancelAll).toHaveBeenCalledTimes(1);
    expect(trading.flattenAll).toHaveBeenCalledTimes(1);
    expect(a).toMatchObject({ cancelledOrders: 0, closedPositions: 1, risk: { killSwitch: true } });
    expect(notices().at(-1)).toMatchObject({ level: "error", title: "Kill switch: action needed" });
    expect(notices().at(-1)!.message).toContain("cancel failed: exchange down");
  });

  it("live: also runs the exchange cancel-all for the current / next market; a failure is reported", async () => {
    const { svc, deps, notices } = setup();
    const cancelAllRemote = vi.fn(async () => ["x1"]);
    (deps.services as { live?: unknown }).live = { executor: () => ({ mode: "live", cancelAllRemote }) };
    await svc.kill();
    expect(cancelAllRemote).toHaveBeenCalledTimes(1);
    expect((cancelAllRemote.mock.calls[0] as unknown[])[0]).toContain("w1");
    cancelAllRemote.mockRejectedValueOnce(new Error("503"));
    await svc.kill();
    expect(notices().at(-1)!.message).toContain("exchange cancel-all failed: 503");
  });

  it("reports positions the flatten could not close", async () => {
    const { svc, trading, notices } = setup();
    trading.flattenAll.mockResolvedValue(0);
    trading.positions.mockReturnValue([{ id: "p1", windowId: "w1", status: "open" }] as never);
    await svc.kill();
    expect(notices().at(-1)!.message).toContain("1 position still open");
  });

  it("a failing risk refresh never stops the kill switch from cancelling and flattening", async () => {
    const { app, svc, trading, notices } = setup();
    svc.refresh();
    trading.riskInputs.mockImplementation(() => {
      throw new Error("db locked");
    });
    const r = await app.inject({ method: "POST", url: "/api/kill" });
    expect(r.statusCode, r.body).toBe(200);
    expect(r.json()).toMatchObject({ cancelledOrders: 2, closedPositions: 1, risk: { killSwitch: true } });
    expect(trading.cancelAll).toHaveBeenCalledWith("kill");
    expect(trading.flattenAll).toHaveBeenCalledWith("kill");
    expect(notices().at(-1)).toMatchObject({ level: "error", title: "Kill switch: action needed" });
    expect(notices().at(-1)!.message).toMatch(/risk state unavailable: db locked/);
    expect(svc.check(entry(1))).toMatchObject({ ok: false, code: "trading_disabled" });
  });

  it("waits for an entry in flight and flattens the position it opens after the first flatten", async () => {
    const { svc, trading, notices } = setup();
    let open: { id: string; windowId: string; status: string }[] = [];
    trading.positions.mockImplementation(() => open as never);
    trading.working.push(order("e1", "buy", "pending"));
    trading.flattenAll.mockImplementation(async () => {
      const n = open.length;
      open = [];
      return n;
    });
    // the in-flight IOC fills a little after the first flatten
    setTimeout(() => {
      trading.working.splice(0);
      open = [{ id: "p-new", windowId: "w1", status: "open" }];
    }, KILL_SETTLE_POLL_MS + 50);
    const r = await svc.kill();
    expect(trading.flattenAll).toHaveBeenCalledTimes(2);
    expect(r).toMatchObject({ closedPositions: 1, risk: { killSwitch: true } });
    expect(notices().at(-1)).toMatchObject({ level: "warn", title: "Kill switch engaged" });
  });

  it("cancels a buy that went resting after cancelAll", async () => {
    const { svc, trading } = setup();
    trading.cancelAll.mockImplementation(async () => {
      trading.working.push(order("late", "buy", "open")); // an in-flight limit order rested after the cancel
      return 0;
    });
    const r = await svc.kill();
    expect(trading.cancelOrder).toHaveBeenCalledWith("late");
    expect(r.cancelledOrders).toBe(1);
  });

  it("PUT /api/risk validates and merges limits through settings", async () => {
    const { app, engine, risks } = setup();
    for (const payload of [{ maxPositionUsd: -1 }, { nope: 1 }, { maxPositionUsd: "5" }, [1]]) {
      const r = await app.inject({ method: "PUT", url: "/api/risk", payload: payload as object });
      expect(r.statusCode, JSON.stringify(payload)).toBe(400);
      expect(r.json().code).toBe("validation");
    }
    expect(engine.updateSettings).not.toHaveBeenCalled();
    const r = await app.inject({ method: "PUT", url: "/api/risk", payload: { maxPositionUsd: 42, dailyLossLimitUsd: 75 } });
    expect(r.statusCode).toBe(200);
    expect(engine.updateSettings).toHaveBeenCalledWith({ risk: { maxPositionUsd: 42, dailyLossLimitUsd: 75 } });
    expect(r.json().limits).toEqual({ ...DEFAULT_RISK_LIMITS, maxPositionUsd: 42, dailyLossLimitUsd: 75 });
    expect(risks().at(-1)!.limits.maxPositionUsd).toBe(42);
    const g = await app.inject({ method: "GET", url: "/api/risk" });
    expect(g.json().limits.dailyLossLimitUsd).toBe(75);
  });

  it("lowering the daily limit below today's loss disables trading at once", async () => {
    const { app, trading } = setup();
    trading.inputs.dailyRealizedPnlUsd = -60;
    const r = await app.inject({ method: "PUT", url: "/api/risk", payload: { dailyLossLimitUsd: 50 } });
    expect(r.json().tradingDisabledUntil).toBe(Date.parse("2026-10-21T04:00:00Z"));
  });
});

// ------------------------------------------------------------------ wired into the real trading module

describe("risk: integration with trading (createModules)", () => {
  it("trading's POST /api/orders is rejected by the limits (403) and the kill switch (423)", async () => {
    const { Engine } = await import("../src/engine.ts");
    const { createModules } = await import("../src/modules/index.ts");
    const { Recorder } = await import("../src/recorder.ts");
    const { loadConfig } = await import("../src/config.ts");
    const now = Date.now();
    const win: MarketWindow = {
      id: "w1", title: "w1", startTs: now - 60_000, endTs: now + 600_000, priceToBeat: 67_000,
      upInstrument: "m:long", downInstrument: "m:short", status: "live", result: null, settlePrice: null,
    };
    const btc = Object.assign(new EventEmitter(), { start() {}, stop() {}, status: () => [], last: () => null });
    const tracker = Object.assign(new EventEmitter(), {
      start() {}, stop() {}, current: () => win, next: () => null, lastOdds: () => null,
      status: () => ({ id: "polymarket" as const, state: "live" as const, lastMsgTs: null, ageMs: null }),
    });
    const db = openDb(":memory:");
    const config = loadConfig({ DB_PATH: ":memory:" } as NodeJS.ProcessEnv);
    const logger = pino({ level: "silent" });
    const engine = new Engine({ config, db, recorder: new Recorder(db), logger, btc, tracker, fetchBars: null, settlements: null, books: null });
    const modules = createModules({ config, db, engine, logger });
    engine.extendSnapshot(() => modules.snapshot());
    const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => modules.register(a), feedsIntervalMs: 60_000 });
    engine.start();
    modules.start();
    cleanup.push(async () => {
      await app.close();
      await modules.stop();
      engine.stop();
      db.close();
    });

    const order = { side: "up", type: "limit", usd: 30, limitCents: 40, maxSlippageCents: 2 };
    expect((await app.inject({ method: "PUT", url: "/api/risk", payload: { maxPositionUsd: 10 } })).statusCode).toBe(200);
    const r1 = await app.inject({ method: "POST", url: "/api/orders", payload: order });
    expect(r1.statusCode, r1.body).toBe(403);
    expect(r1.json()).toMatchObject({ code: "risk_rejected", error: "Max position $10" });

    expect((await app.inject({ method: "PUT", url: "/api/risk", payload: { maxPositionUsd: 250 } })).statusCode).toBe(200);
    const k = await app.inject({ method: "POST", url: "/api/kill" });
    expect(k.json()).toMatchObject({ risk: { killSwitch: true } });
    const r2 = await app.inject({ method: "POST", url: "/api/orders", payload: order });
    expect(r2.statusCode, r2.body).toBe(423);
    expect(r2.json()).toMatchObject({ code: "trading_disabled", error: "Kill switch engaged" });
    expect(engine.snapshot().risk?.killSwitch).toBe(true);
  });
});
