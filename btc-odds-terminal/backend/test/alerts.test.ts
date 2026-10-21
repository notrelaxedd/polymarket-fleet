// Alerts (backend/src/alerts): the crossing / hysteresis / cooldown evaluator, evaluation on engine updates,
// the alert_events log + alertFired broadcast, `once`, persistence, and the CRUD routes.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type { Alert, AlertEvent, AlertKind, BtcTick, FairSnap, MarketWindow, OddsSnap, ServerMsg, Settings, StateSnapshot } from "../../shared/protocol.ts";
import type { AlertSample } from "../../shared/alerts.ts";
import { ALERT_COOLDOWN_MS, AlertEvaluator, createAlerts, downMidOf, hysteresisBand } from "../src/alerts/index.ts";
import { describeAlert } from "../src/alerts/message.ts";
import type { Config } from "../src/config.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import type { EngineApi, ModuleDeps, Services } from "../src/modules/types.ts";
import { buildServer } from "../src/server/index.ts";

const T0 = Date.parse("2026-10-20T18:00:00Z");

// ------------------------------------------------------------------ evaluator

const alert = (kind: AlertKind, threshold: number | null, over: Partial<Alert> = {}): Alert => ({
  id: `${kind}:${threshold}`,
  kind,
  threshold,
  enabled: true,
  once: false,
  note: null,
  lastFiredTs: null,
  createdTs: 0,
  updatedTs: 0,
  ...over,
});

const smp = (over: Partial<AlertSample>, ts = T0): AlertSample => ({
  ts,
  windowId: "w1",
  btc: null,
  upMid: null,
  downMid: null,
  edge: null,
  ptb: null,
  ...over,
});

/** Feed values through one alert; returns the indexes of the samples that fired (lastFiredTs kept like the module). */
function run(a: Alert, samples: AlertSample[], ev = new AlertEvaluator()): number[] {
  const fired: number[] = [];
  samples.forEach((s, i) => {
    const f = ev.step(a, s);
    if (f) {
      fired.push(i);
      a.lastFiredTs = s.ts;
    }
  });
  return fired;
}

const btcSeries = (values: (number | null)[], stepMs = 1000) => values.map((btc, i) => smp({ btc }, T0 + i * stepMs));

describe("alert evaluator", () => {
  it("fires on the crossing only, not while the value stays beyond the threshold", () => {
    const a = alert("btc_above", 70_000);
    expect(run(a, btcSeries([69_950, 69_990, 70_000, 70_050, 70_100, 70_200]))).toEqual([2]);
  });

  it("does not fire when the condition already holds at creation; must come back and cross", () => {
    const a = alert("btc_above", 70_000);
    // band at 70k is 0.02% = $14: 69_990 is not far enough to re-arm, 69_980 is
    expect(hysteresisBand("btc_above", 70_000)).toBeCloseTo(14);
    expect(run(a, btcSeries([70_100, 70_050, 69_990, 70_010, 69_980, 70_001], 40_000))).toEqual([5]);
  });

  it("hysteresis: noise around the level does not re-fire until the value retreats past the band", () => {
    const a = alert("btc_below", 65_000);
    const ev = new AlertEvaluator(0); // no cooldown: isolate the band
    const fired = run(a, btcSeries([65_100, 64_999, 65_005, 64_990, 65_010, 64_995, 65_020, 64_900]), ev);
    // 64_999 fires; re-arms only above 65_013 (65_020), so the next fire is 64_900
    expect(fired).toEqual([1, 7]);
  });

  it("cooldown suppresses a re-cross within ALERT_COOLDOWN_MS of the last fire", () => {
    const a = alert("up_above", 60);
    const ev = new AlertEvaluator();
    const s = (upMid: number, dt: number) => smp({ upMid }, T0 + dt);
    expect(ev.step(a, s(55, 0))).toBeNull();
    expect(ev.step(a, s(61, 1000))).not.toBeNull();
    a.lastFiredTs = T0 + 1000;
    expect(ev.step(a, s(57, 2000))).toBeNull(); // re-armed (57 < 59)
    expect(ev.step(a, s(62, 3000))).toBeNull(); // crossing inside the cooldown: suppressed (consumed)
    expect(ev.step(a, s(63, 1000 + ALERT_COOLDOWN_MS + 1))).toBeNull(); // no late fire for the suppressed cross
    expect(ev.step(a, s(55, 1000 + ALERT_COOLDOWN_MS + 2))).toBeNull();
    expect(ev.step(a, s(60, 1000 + ALERT_COOLDOWN_MS + 3))).toMatchObject({ value: 60 });
  });

  it("odds and edge alerts re-initialise on a new window (no cross-window jump fires)", () => {
    const a = alert("up_above", 80);
    const ev = new AlertEvaluator(0);
    expect(ev.step(a, smp({ upMid: 70, windowId: "w1" }))).toBeNull();
    expect(ev.step(a, smp({ upMid: 85, windowId: "w2" }))).toBeNull();
    expect(ev.step(a, smp({ upMid: 70, windowId: "w2" }))).toBeNull();
    expect(ev.step(a, smp({ upMid: 81, windowId: "w2" }))).toMatchObject({ value: 81, windowId: "w2" });

    const e = alert("edge_below", -5);
    expect(run(e, [-2, -4, -5.5, -6, -3.9, -5.1].map((edge, i) => smp({ edge }, T0 + i * 40_000)), ev)).toEqual([2, 5]);
  });

  it("down alerts use the Down mid; a missing value changes nothing", () => {
    const a = alert("down_below", 30);
    const ev = new AlertEvaluator(0);
    expect(ev.step(a, smp({ downMid: 35 }))).toBeNull();
    expect(ev.step(a, smp({ downMid: null }))).toBeNull();
    expect(ev.step(a, smp({ downMid: 29 }))).toMatchObject({ value: 29 });
    expect(downMidOf({ ts: 0, windowId: "w", upBid: 60, upAsk: 62, downBid: null, downAsk: 40, upMid: 61 })).toBe(39);
    expect(downMidOf({ ts: 0, windowId: "w", upBid: 60, upAsk: 62, downBid: 37, downAsk: 41, upMid: 61 })).toBe(39);
  });

  it("btc_cross_ptb fires on each side flip outside the dead band, within one window", () => {
    const a = alert("btc_cross_ptb", null);
    const ev = new AlertEvaluator(0);
    const s = (btc: number, windowId = "w1") => smp({ btc, ptb: 67_000, windowId });
    expect(ev.step(a, s(67_010))).toBeNull(); // initial side: above
    expect(ev.step(a, s(66_999))).toBeNull(); // inside the $3 dead band
    expect(ev.step(a, s(67_001))).toBeNull();
    expect(ev.step(a, s(66_996))).toMatchObject({ direction: "below", value: 66_996 });
    expect(ev.step(a, s(66_990))).toBeNull();
    expect(ev.step(a, s(67_004))).toMatchObject({ direction: "above", value: 67_004 });
    // new window, new PTB: re-initialise, no fire for the jump
    expect(ev.step(a, s(66_900, "w2"))).toBeNull();
    expect(ev.step(a, s(67_100, "w2"))).toMatchObject({ direction: "above", windowId: "w2" });
    // no PTB yet: nothing
    expect(ev.step(a, smp({ btc: 60_000, ptb: null, windowId: "w2" }))).toBeNull();
  });

  it("btc_cross_ptb: a PTB revised within the window re-reads the side without firing", () => {
    const a = alert("btc_cross_ptb", null);
    const ev = new AlertEvaluator(0);
    expect(ev.step(a, smp({ btc: 67_010, ptb: 67_000 }))).toBeNull(); // above the BRTI-proxy PTB
    // the metadata PTB replaces the proxy: BTC did not move, but is now below it -> no fire
    expect(ev.step(a, smp({ btc: 67_010, ptb: 67_050 }))).toBeNull();
    expect(ev.step(a, smp({ btc: 67_012, ptb: 67_050 }))).toBeNull();
    // a real crossing of the revised PTB still fires
    expect(ev.step(a, smp({ btc: 67_060, ptb: 67_050 }))).toMatchObject({ direction: "above", value: 67_060 });
  });

  it("an edited definition re-initialises; disabled alerts are skipped", () => {
    const ev = new AlertEvaluator(0);
    const a = alert("btc_above", 70_000);
    ev.step(a, smp({ btc: 69_000 }));
    a.threshold = 68_000; // already beyond the new threshold: must not fire
    expect(ev.step(a, smp({ btc: 69_100 }))).toBeNull();
    const off = alert("btc_above", 60_000, { enabled: false });
    expect(ev.evaluate([off], smp({ btc: 59_000 }))).toEqual([]);
    expect(ev.evaluate([off], smp({ btc: 61_000 }))).toEqual([]);
  });

  it("describes alerts in their units", () => {
    expect(describeAlert({ kind: "btc_above", threshold: 70_000 })).toBe("BTC above $70,000.00");
    expect(describeAlert({ kind: "up_below", threshold: 40 })).toBe("Up below 40.0¢");
    expect(describeAlert({ kind: "edge_above", threshold: 5 })).toBe("Edge above +5.0¢");
    expect(describeAlert({ kind: "btc_cross_ptb", threshold: null })).toBe("BTC crosses price to beat");
  });
});

// ------------------------------------------------------------------ module

class FakeEngine extends EventEmitter {
  tick: BtcTick | null = null;
  odds: OddsSnap | null = null;
  fair: FairSnap | null = null;
  window: MarketWindow = {
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
  settings: Settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "" };
  getSettings = () => this.settings;
  updateSettings = () => this.settings;
  currentWindow = () => this.window;
  nextWindow = () => null;
  latestTick = () => this.tick;
  latestOdds = () => this.odds;
  latestFair = () => this.fair;
  currentBook = () => null;
  refreshBook = async () => null;
  priceAt = () => null;
  liveAvailable = () => false;
  feeds = () => [];
  snapshot = () => ({}) as StateSnapshot;
}

const cleanup: (() => unknown)[] = [];
afterEach(async () => {
  for (const f of cleanup.splice(0)) await f();
});

function setup(opts: { db?: TerminalDb; now?: number } = {}) {
  const db = opts.db ?? openDb(":memory:");
  const engine = new FakeEngine();
  const msgs: ServerMsg[] = [];
  const clock = { now: opts.now ?? T0 };
  const deps: ModuleDeps = {
    config: {} as Config,
    db,
    engine: engine as unknown as EngineApi,
    broadcast: (m) => msgs.push(m),
    logger: pino({ level: "silent" }),
    now: () => clock.now,
    services: {} as Services,
  };
  const mod = createAlerts(deps);
  const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => mod.register(a), feedsIntervalMs: 60_000 });
  mod.start?.();
  cleanup.push(async () => {
    await app.close();
    await mod.stop?.();
  });
  const fired = () => msgs.filter((m): m is { type: "alertFired"; data: AlertEvent } => m.type === "alertFired").map((m) => m.data);
  const lists = () => msgs.filter((m): m is { type: "alerts"; data: Alert[] } => m.type === "alerts").map((m) => m.data);
  /** advance the clock, set BTC, emit a tick like the engine does */
  const btc = (price: number, dt = 1000) => {
    clock.now += dt;
    engine.tick = { ts: clock.now, price, sources: {} };
    engine.emit("tick", engine.tick);
  };
  const create = async (payload: object) => {
    const r = await app.inject({ method: "POST", url: "/api/alerts", payload });
    expect(r.statusCode, r.body).toBe(200);
    return r.json() as Alert;
  };
  return { db, engine, msgs, clock, mod, app, fired, lists, btc, create };
}

describe("alerts module", () => {
  it("CRUD: create (defaults), list, partial update, delete; every change broadcasts the full list", async () => {
    const { app, lists, create } = setup();
    const a = await create({ kind: "btc_above", threshold: 70_000 });
    expect(a).toMatchObject({ kind: "btc_above", threshold: 70_000, enabled: true, once: false, note: null, lastFiredTs: null, createdTs: T0 });
    const p = await create({ kind: "btc_cross_ptb", threshold: 123, note: "  ptb  " });
    expect(p).toMatchObject({ threshold: null, note: "ptb" });
    expect(lists().at(-1)!.map((x) => x.id)).toEqual([a.id, p.id]);

    const u = await app.inject({ method: "PUT", url: `/api/alerts/${a.id}`, payload: { threshold: 71_000, once: true } });
    expect(u.json()).toMatchObject({ id: a.id, kind: "btc_above", threshold: 71_000, once: true, enabled: true });
    const u2 = await app.inject({ method: "PUT", url: `/api/alerts/${a.id}`, payload: { kind: "up_above" } });
    expect(u2.statusCode).toBe(400); // 71_000 is not an odds threshold
    const u3 = await app.inject({ method: "PUT", url: `/api/alerts/${a.id}`, payload: { kind: "up_above", threshold: 65 } });
    expect(u3.json()).toMatchObject({ kind: "up_above", threshold: 65 });

    expect((await app.inject({ method: "GET", url: "/api/alerts" })).json()).toHaveLength(2);
    const d = await app.inject({ method: "DELETE", url: `/api/alerts/${p.id}` });
    expect(d.json()).toEqual({ ok: true });
    expect(lists().at(-1)!.map((x) => x.id)).toEqual([a.id]);
    expect((await app.inject({ method: "DELETE", url: `/api/alerts/${p.id}` })).json()).toMatchObject({ code: "not_found" });
    expect((await app.inject({ method: "PUT", url: "/api/alerts/nope", payload: {} })).statusCode).toBe(404);
  });

  it("validates input", async () => {
    const { app } = setup();
    const bad = [
      { kind: "btc_above" }, // threshold required
      { kind: "btc_above", threshold: -1 },
      { kind: "up_above", threshold: 100 },
      { kind: "down_below", threshold: 0 },
      { kind: "edge_above", threshold: 150 },
      { kind: "nope", threshold: 1 },
      { kind: "btc_above", threshold: 1, extra: true },
      { kind: "btc_above", threshold: "70000" },
      { kind: "btc_above", threshold: 1, note: "x".repeat(201) },
    ];
    for (const payload of bad) {
      const r = await app.inject({ method: "POST", url: "/api/alerts", payload });
      expect(r.statusCode, JSON.stringify(payload)).toBe(400);
      expect(r.json().code).toBe("validation");
    }
    expect((await app.inject({ method: "GET", url: "/api/alerts/events?limit=0" })).statusCode).toBe(400);
  });

  it("evaluates on engine ticks: logs alert_events, broadcasts alertFired + the list, honours cooldown", async () => {
    const { app, fired, btc, create, lists, mod } = setup();
    const a = await create({ kind: "btc_above", threshold: 70_000, note: "breakout" });
    btc(69_990);
    btc(70_010);
    expect(fired()).toHaveLength(1);
    const ev = fired()[0]!;
    expect(ev).toMatchObject({ alertId: a.id, kind: "btc_above", windowId: "w1", value: 70_010, ts: T0 + 2000 });
    expect(ev.message).toBe("BTC above $70,000.00 (now $70,010.00) · breakout");
    expect(lists().at(-1)!.find((x) => x.id === a.id)!.lastFiredTs).toBe(T0 + 2000);

    btc(70_050);
    btc(69_900); // re-arm
    btc(70_001); // inside the cooldown: suppressed
    expect(fired()).toHaveLength(1);
    btc(69_900, ALERT_COOLDOWN_MS);
    btc(70_020);
    expect(fired()).toHaveLength(2);

    const events = (await app.inject({ method: "GET", url: "/api/alerts/events?limit=10" })).json() as AlertEvent[];
    expect(events.map((e) => e.value)).toEqual([70_020, 70_010]); // newest first
    expect((await app.inject({ method: "GET", url: `/api/alerts/events?alertId=${a.id}&limit=1` })).json()).toHaveLength(1);
    expect(mod.snapshot!()).toMatchObject({ alerts: [{ id: a.id }], alertEvents: [{ value: 70_020 }, { value: 70_010 }] });
  });

  it("`once` alerts disable themselves after firing", async () => {
    const { fired, btc, create, lists } = setup();
    const a = await create({ kind: "btc_below", threshold: 66_000, once: true });
    btc(66_100);
    btc(65_990);
    btc(66_200);
    btc(65_000, ALERT_COOLDOWN_MS * 2);
    expect(fired()).toHaveLength(1);
    expect(lists().at(-1)!.find((x) => x.id === a.id)).toMatchObject({ enabled: false, lastFiredTs: T0 + 2000 });
  });

  it("a failed alert write leaves the in-memory alert untouched (memory and DB stay in step)", async () => {
    const { db, fired, btc, create, app } = setup();
    const a = await create({ kind: "btc_below", threshold: 66_000, once: true });
    btc(66_100);
    db.raw.exec("CREATE TRIGGER alerts_fail BEFORE UPDATE ON alerts BEGIN SELECT RAISE(ABORT, 'disk full'); END");
    btc(65_990);
    expect(fired()).toHaveLength(0);
    const listed = (await app.inject({ method: "GET", url: "/api/alerts" })).json() as Alert[];
    expect(listed.find((x) => x.id === a.id)).toMatchObject({ enabled: true, lastFiredTs: null });
    const row = db.raw.prepare("SELECT enabled, last_fired_ts FROM alerts WHERE id = ?").get(a.id);
    expect(row).toEqual({ enabled: 1, last_fired_ts: null });
    expect((await app.inject({ method: "GET", url: "/api/alerts/events" })).json()).toHaveLength(0);
  });

  it("samples odds, Down mid and edge from the engine (odds / fair events) and BTC vs PTB", async () => {
    const { engine, clock, fired, create, btc } = setup();
    await create({ kind: "up_above", threshold: 60 });
    await create({ kind: "down_below", threshold: 40 });
    await create({ kind: "edge_above", threshold: 5 });
    await create({ kind: "btc_cross_ptb", threshold: null });
    const quote = (upMid: number, edge: number) => {
      clock.now += 1000;
      engine.odds = { ts: clock.now, windowId: "w1", upBid: upMid - 1, upAsk: upMid + 1, downBid: null, downAsk: null, upMid };
      engine.emit("odds", engine.odds);
      engine.fair = { ts: clock.now, windowId: "w1", fair: upMid + edge, sigma: 0.0001, tLeftSec: 600, edge };
      engine.emit("fair", engine.fair);
    };
    quote(55, 2);
    quote(62, 6);
    expect(fired().map((e) => e.kind).sort()).toEqual(["down_below", "edge_above", "up_above"]);
    expect(fired().find((e) => e.kind === "down_below")!.message).toBe("Down below 40.0¢ (mid 38.0¢)");
    expect(fired().find((e) => e.kind === "edge_above")!.message).toBe("Edge above +5.0¢ (now +6.0¢)");

    btc(67_020);
    btc(66_980);
    const ptb = fired().find((e) => e.kind === "btc_cross_ptb")!;
    expect(ptb.message).toBe("BTC crossed below price to beat $67,000.00 (now $66,980.00)");
  });

  it("ignores a stale BTC tick", async () => {
    const { engine, clock, fired, create, mod } = setup();
    await create({ kind: "btc_above", threshold: 70_000 });
    engine.tick = { ts: clock.now, price: 69_000, sources: {} };
    mod.evaluate();
    clock.now += 10_000; // tick is 10s old
    engine.tick = { ts: clock.now - 10_000, price: 71_000, sources: {} };
    expect(mod.sample().btc).toBeNull();
    expect(mod.evaluate()).toEqual([]);
    expect(fired()).toHaveLength(0);
  });

  it("persists alerts and lastFiredTs across restarts (cooldown survives)", async () => {
    const a = setup();
    const al = await a.create({ kind: "btc_above", threshold: 70_000 });
    a.btc(69_000);
    a.btc(70_500);
    expect(a.fired()).toHaveLength(1);
    await a.mod.stop?.();

    const b = setup({ db: a.db, now: a.clock.now });
    expect(b.mod.service.list()).toMatchObject([{ id: al.id, lastFiredTs: a.clock.now }]);
    expect(b.mod.service.events()).toHaveLength(1);
    b.btc(69_000);
    b.btc(70_500); // a crossing 2s after the last fire: cooldown
    expect(b.fired()).toHaveLength(0);
  });
});
