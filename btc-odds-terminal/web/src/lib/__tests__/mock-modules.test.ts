// Web mock feature modules that mirror the backend: analysis (shared math, non-empty), replay payload shape,
// alerts evaluation (crossing / once / cooldown via shared/alert-eval) and the risk policy (daily-loss latch,
// override, kill switch re-arm rules).
import { test } from "node:test";
import assert from "node:assert/strict";
import type { Alert, AlertEvent, AnalysisPayload, MarketWindow, OddsSnap, ReplayData, RiskState, ServerMsg, Settings } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { DAILY_LOSS_CONFIRM_MS } from "@shared/risk";
import { ApiError } from "../api-error";
import { getMock } from "../mock";
import { createMockAlerts } from "../mock/alerts";
import { createMockRisk } from "../mock/risk";
import { mockRequest } from "../mock/router";
import type { MockCtx, MockModule, MockRequest } from "../mock/types";

test("mock analysis runs the shared math on the simulator's history (not the empty stub)", async () => {
  getMock();
  const p = await mockRequest<AnalysisPayload>("GET", "/analysis?scope=last20");
  assert.ok(p.windowIds.length > 1 && p.windowIds.length <= 20);
  assert.ok(p.scatter.length > 0, "scatter points");
  assert.ok(p.fairCurve.length > 0, "fair curve");
  assert.ok(p.lag.some((b) => b.rho != null), "lag bars");
  assert.notEqual(p.takeaways.lag, "Not enough data yet.");
  await assert.rejects(mockRequest("GET", "/analysis?scope=nope"), (e: unknown) => e instanceof ApiError && e.status === 400);
});

test("mock replay matches the backend payload: 15 min lead-in, frames, ticks", async () => {
  const mock = getMock();
  const w = mock.snapshot().recent[0];
  const d = await mockRequest<ReplayData>("GET", `/replay/${encodeURIComponent(w.id)}`);
  assert.equal(d.window.id, w.id);
  assert.ok(d.bars[0].t <= w.startTs - 14 * 60_000, "bars start ~15 min before the window");
  assert.ok(d.frames.length >= 890, `frames ${d.frames.length}`);
  assert.ok((d.ticks ?? []).length > 0, "ticks");
  assert.ok(d.odds.every((o) => o.windowId === w.id));
  await assert.rejects(mockRequest("GET", "/replay/nope"), (e: unknown) => e instanceof ApiError && e.status === 404);
});

// ---------------------------------------------------------------- fake ctx for alerts / risk

const T0 = Date.parse("2026-10-20T16:00:00Z");
const win: MarketWindow = {
  id: "w1",
  title: "w1",
  startTs: T0 - 60_000,
  endTs: T0 + 840_000,
  priceToBeat: 67_000,
  upInstrument: "w1:up",
  downInstrument: "w1:down",
  status: "live",
  result: null,
  settlePrice: null,
};

function fake() {
  const st = {
    now: T0,
    btc: 66_900,
    upMid: 50 as number | null,
    emitted: [] as ServerMsg[],
    settings: { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "", risk: { ...DEFAULT_RISK_LIMITS } } as Settings,
    inputs: { dailyRealizedPnlUsd: 0, unrealizedPnlUsd: 0, openExposureUsd: 0, windowLossUsedUsd: 0 },
    cancelled: 0,
  };
  const ctx = {
    emit: (m: ServerMsg) => st.emitted.push(m),
    now: () => st.now,
    current: () => win,
    next: () => win,
    getWindow: () => win,
    windows: () => [win],
    lastTick: () => ({ ts: st.now, price: st.btc, sources: {} }),
    lastOdds: (): OddsSnap => ({ ts: st.now, windowId: "w1", upBid: null, upAsk: null, downBid: null, downAsk: null, upMid: st.upMid }),
    lastFair: () => null,
    book: () => null,
    settings: () => st.settings,
    putSettings: (p: { risk?: Partial<Settings["risk"]> }) => {
      st.settings = { ...st.settings, risk: { ...st.settings.risk!, ...(p.risk ?? {}) } };
      return st.settings;
    },
    bars: () => [],
    odds: () => [],
    fair: () => [],
    ticks: () => [],
    priceAt: () => st.btc,
  } as unknown as MockCtx;
  ctx.modules = {
    trading: {
      riskInputs: () => st.inputs,
      cancelAll: () => 0,
      flattenAll: () => 0,
      positions: () => [],
      cancelRestingBuys: () => ++st.cancelled,
    },
  } as unknown as MockCtx["modules"];
  return { st, ctx };
}

const route = (m: MockModule, method: string, path: string) => {
  const r = m.routes!.find((x) => x.method === method && x.path === path)!;
  return (req: Partial<MockRequest> = {}) => r.handler({ params: {}, query: {}, body: null, ...req });
};
const isBad = (e: unknown) => e instanceof ApiError && e.status === 400;

test("mock alerts: fire on a crossing only, `once` switches off, cooldown, validation", async () => {
  const { st, ctx } = fake();
  const m = createMockAlerts(ctx);
  const post = route(m, "POST", "/alerts");
  await assert.rejects(Promise.resolve().then(() => post({ body: { kind: "up_above", threshold: 140 } })), isBad);
  await assert.rejects(Promise.resolve().then(() => post({ body: { kind: "nope", threshold: 1 } })), isBad);
  const up = (await post({ body: { kind: "up_above", threshold: 60 } })) as Alert;
  const fired = () => st.emitted.filter((x): x is { type: "alertFired"; data: AlertEvent } => x.type === "alertFired").map((x) => x.data);
  const step = (upMid: number, dt = 1000) => {
    st.now += dt;
    st.upMid = upMid;
    m.onSecond!(st.now);
  };
  step(55); // initialises the arm state
  step(61);
  assert.equal(fired().filter((e) => e.alertId === up.id).length, 1);
  step(62);
  step(63);
  assert.equal(fired().filter((e) => e.alertId === up.id).length, 1, "no repeat while beyond");
  step(55);
  step(61); // inside the 30s cooldown
  assert.equal(fired().filter((e) => e.alertId === up.id).length, 1, "cooldown");
  const once = (await post({ body: { kind: "btc_above", threshold: 67_000, once: true } })) as Alert;
  st.btc = 66_950;
  step(50);
  st.btc = 67_010;
  step(50);
  const ev = fired().find((e) => e.alertId === once.id)!;
  assert.match(ev.message, /BTC above \$67,000\.00/);
  assert.equal(m.list().find((a) => a.id === once.id)!.enabled, false);
  assert.equal(m.list().find((a) => a.id === once.id)!.lastFiredTs, ev.ts);
});

test("mock risk: realized loss latches at once, unrealized only after the confirm delay; override and kill rules", async () => {
  const { st, ctx } = fake();
  const m = createMockRisk(ctx);
  const put = route(m, "PUT", "/risk");
  st.settings.risk!.dailyLossLimitUsd = 50;
  // unrealized at the limit: entries paused, not latched until DAILY_LOSS_CONFIRM_MS
  st.inputs = { ...st.inputs, unrealizedPnlUsd: -60 };
  assert.equal(m.refresh().tradingDisabledUntil, null);
  assert.equal(m.check({ addCostUsd: 1, windowCostUsd: 0, positionCostUsd: 0, reducing: false }).ok, false);
  st.now += DAILY_LOSS_CONFIRM_MS;
  assert.ok(m.refresh().tradingDisabledUntil != null);
  assert.equal(st.cancelled, 1, "resting buys cancelled on the latch");
  // override refused while at the limit; allowed with a higher limit in the same PUT
  await assert.rejects(Promise.resolve().then(() => put({ body: { clearDisable: true } })), isBad);
  const s = (await put({ body: { clearDisable: true, dailyLossLimitUsd: 100 } })) as RiskState;
  assert.equal(s.tradingDisabledUntil, null);
  // realized loss alone latches immediately
  st.inputs = { ...st.inputs, unrealizedPnlUsd: 0, dailyRealizedPnlUsd: -100 };
  assert.ok(m.refresh().tradingDisabledUntil != null);
  // limit validation and kill switch rules
  await assert.rejects(Promise.resolve().then(() => put({ body: { dailyLossLimitUsd: 0 } })), isBad);
  await assert.rejects(Promise.resolve().then(() => put({ body: { killSwitch: true } })), isBad);
  assert.equal(m.kill().risk.killSwitch, true);
  assert.equal(((await put({ body: {} })) as RiskState).killSwitch, true, "stays on until re-armed");
  assert.equal(((await put({ body: { killSwitch: false } })) as RiskState).killSwitch, false);
});
