// Drawings (backend/src/drawings): CRUD routes, validation, id handling, persistence, `drawings` broadcasts.
import { EventEmitter } from "node:events";
import pino from "pino";
import { afterEach, describe, expect, it } from "vitest";
import type { Drawing, ServerMsg, Settings, StateSnapshot } from "../../shared/protocol.ts";
import type { Config } from "../src/config.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import { createDrawings } from "../src/drawings/index.ts";
import type { EngineApi, ModuleDeps, Services } from "../src/modules/types.ts";
import { buildServer } from "../src/server/index.ts";

const T0 = Date.parse("2026-10-20T18:00:00Z");

class FakeEngine extends EventEmitter {
  settings: Settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "" };
  getSettings = () => this.settings;
  updateSettings = () => this.settings;
  currentWindow = () => null;
  nextWindow = () => null;
  latestTick = () => null;
  latestOdds = () => null;
  latestFair = () => null;
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

function setup(db: TerminalDb = openDb(":memory:")) {
  const engine = new FakeEngine();
  const msgs: ServerMsg[] = [];
  const clock = { now: T0 };
  const deps: ModuleDeps = {
    config: {} as Config,
    db,
    engine: engine as unknown as EngineApi,
    broadcast: (m) => msgs.push(m),
    logger: pino({ level: "silent" }),
    now: () => clock.now,
    services: {} as Services,
  };
  const mod = createDrawings(deps);
  const app = buildServer(engine, db, { corsOrigin: "*" }, { register: (a) => mod.register(a), feedsIntervalMs: 60_000 });
  cleanup.push(() => app.close());
  const lists = () => msgs.filter((m): m is { type: "drawings"; data: Drawing[] } => m.type === "drawings").map((m) => m.data);
  return { db, app, mod, lists, clock };
}

const trend = {
  kind: "trend",
  points: [
    { time: T0 - 60_000, price: 67_000, scale: "btc" },
    { time: T0, price: 67_100, scale: "btc" },
  ],
  color: "#3B82F6",
  locked: false,
  label: null,
};
const sticker = { kind: "sticker", points: [{ time: T0, price: 55, scale: "odds" }], color: "#F59E0B", locked: false, label: null, sticker: "rocket" };

describe("drawings module", () => {
  it("create / list / update / delete, broadcasting the full list each time", async () => {
    const { app, lists } = setup();
    const a = (await app.inject({ method: "POST", url: "/api/drawings", payload: trend })).json() as Drawing;
    expect(a).toMatchObject({ kind: "trend", createdTs: T0, updatedTs: T0 });
    const b = await app.inject({ method: "POST", url: "/api/drawings", payload: { ...sticker, id: "client-1" } });
    expect(b.json().id).toBe("client-1");
    expect((await app.inject({ method: "GET", url: "/api/drawings" })).json().map((d: Drawing) => d.id)).toEqual([a.id, "client-1"]);
    const u = await app.inject({ method: "PUT", url: `/api/drawings/${a.id}`, payload: { color: "#EF4444" } });
    expect(u.statusCode).toBe(200);
    expect(u.json()).toMatchObject({ color: "#EF4444", kind: "trend", createdTs: T0 });
    expect(u.json().updatedTs).toBeGreaterThan(a.updatedTs);
    expect((await app.inject({ method: "DELETE", url: `/api/drawings/${a.id}` })).json()).toEqual({ ok: true });
    expect(lists().length).toBe(4);
    expect(lists().at(-1)!.map((d) => d.id)).toEqual(["client-1"]);
  });

  it("returns 404 for unknown ids and 400 for invalid drawings", async () => {
    const { app } = setup();
    expect((await app.inject({ method: "PUT", url: "/api/drawings/nope", payload: { color: "#fff" } })).json()).toMatchObject({ code: "not_found" });
    expect((await app.inject({ method: "DELETE", url: "/api/drawings/nope" })).statusCode).toBe(404);
    const bad = [
      { ...trend, points: trend.points.slice(0, 1) },
      { ...trend, points: [trend.points[0], { ...trend.points[1], scale: "odds" }] },
      { ...trend, color: "red" },
      { ...trend, kind: "circle" },
      { ...sticker, sticker: undefined },
      { ...trend, points: [{ time: Number.NaN, price: 1, scale: "btc" }, trend.points[1]] },
    ];
    for (const p of bad) {
      const r = await app.inject({ method: "POST", url: "/api/drawings", payload: p });
      expect(r.statusCode, JSON.stringify(p)).toBe(400);
      expect(r.json().code).toBe("validation");
    }
  });

  it("does not reuse a taken client id; replace-all keeps given ids; delete-all", async () => {
    const { app } = setup();
    await app.inject({ method: "POST", url: "/api/drawings", payload: { ...trend, id: "x1" } });
    const dup = (await app.inject({ method: "POST", url: "/api/drawings", payload: { ...trend, id: "x1" } })).json();
    expect(dup.id).not.toBe("x1");
    const all = (await app.inject({ method: "GET", url: "/api/drawings" })).json() as Drawing[];
    expect((await app.inject({ method: "DELETE", url: "/api/drawings" })).json()).toEqual({ deleted: 2 });
    const back = await app.inject({ method: "PUT", url: "/api/drawings", payload: all });
    expect(back.statusCode).toBe(200);
    expect(back.json().map((d: Drawing) => d.id)).toEqual(all.map((d) => d.id));
    const dupIds = await app.inject({ method: "PUT", url: "/api/drawings", payload: [all[0], all[0]] });
    expect(dupIds.statusCode).toBe(400);
  });

  it("persists across restarts and appears in the snapshot", async () => {
    const db = openDb(":memory:");
    const first = setup(db);
    await first.app.inject({ method: "POST", url: "/api/drawings", payload: { ...sticker, id: "s1" } });
    const second = setup(db);
    expect(second.mod.service.list().map((d) => d.id)).toEqual(["s1"]);
    expect(second.mod.snapshot?.()).toMatchObject({ drawings: [{ id: "s1", sticker: "rocket", locked: false }] });
  });
});
