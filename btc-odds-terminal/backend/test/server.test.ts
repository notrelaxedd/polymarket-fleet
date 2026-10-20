import { EventEmitter } from "node:events";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import WebSocket from "ws";
import type { FeedStatus, ServerMsg, Settings, StateSnapshot } from "../../shared/protocol.ts";
import { openDb, type TerminalDb } from "../src/db/index.ts";
import { SettingsPatchSchema } from "../src/db/settings.ts";
import { buildServer, MAX_RANGE_MS, parseRange } from "../src/server/index.ts";
import type { EngineLike } from "../src/server/types.ts";

const NOW = Date.now();

class FakeEngine extends EventEmitter implements EngineLike {
  settings: Settings = { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "[FEE] unconfirmed" };
  feedList: FeedStatus[] = [
    { id: "coinbase", state: "live", lastMsgTs: NOW, ageMs: 10 },
    { id: "polymarket", state: "down", lastMsgTs: null, ageMs: null, detail: "offline" },
  ];
  snapshot(): StateSnapshot {
    return {
      serverTime: Date.now(),
      settings: this.settings,
      current: null,
      next: null,
      recent: [],
      lastTick: { ts: NOW, price: 67000, sources: { coinbase: 67000 } },
      lastOdds: null,
      lastFair: null,
      feeds: this.feedList,
    };
  }
  feeds(): FeedStatus[] {
    return this.feedList;
  }
  getSettings(): Settings {
    return this.settings;
  }
  updateSettings(patch: unknown): Settings {
    const p = SettingsPatchSchema.safeParse(patch);
    if (!p.success) throw Object.assign(new Error("invalid settings"), { statusCode: 400 });
    const { feeNote: _ignored, ...rest } = p.data;
    this.settings = { ...this.settings, ...rest };
    this.emit("msg", { type: "settings", data: this.settings } satisfies ServerMsg);
    return this.settings;
  }
}

function seed(db: TerminalDb) {
  const t0 = NOW - 2 * 3_600_000;
  db.insertBarsIgnore([{ t: t0, d: 60, o: 1, h: 2, l: 0.5, c: 1.5 }]);
  db.upsertBars([{ t: NOW - 5000, d: 1, o: 1, h: 1, l: 1, c: 1 }]);
  db.upsertBars([{ t: NOW - 30 * 3_600_000, d: 1, o: 9, h: 9, l: 9, c: 9 }]); // older than 24h
  db.insertOdds([{ ts: NOW - 1000, windowId: "w", upBid: 40, upAsk: 42, downBid: 58, downAsk: 60, upMid: 41 }]);
  db.insertFair([{ ts: NOW - 1000, windowId: "w", fair: 45, sigma: 1e-4, tLeftSec: 300, edge: 4 }]);
  for (let i = 0; i < 5; i++) {
    const s = NOW - (i + 1) * 900_000;
    db.upsertWindow({
      id: `w${i}`, title: "t", startTs: s, endTs: s + 900_000, priceToBeat: 1, upInstrument: "u", downInstrument: "d",
      status: "settled", result: "up", settlePrice: 2,
    });
  }
}

const setup = (corsOrigin: string[] | "*" = "*") => {
  const engine = new FakeEngine();
  const db = openDb(":memory:");
  seed(db);
  const app = buildServer(engine, db, { corsOrigin }, { feedsIntervalMs: 50, heartbeatMs: 1000 });
  return { engine, db, app };
};

let cleanup: (() => Promise<void>) | null = null;
afterEach(async () => {
  await cleanup?.();
  cleanup = null;
});

describe("parseRange", () => {
  it("defaults to the last 3h, caps at 24h, rejects bad input", () => {
    expect(parseRange({}, 10_000_000_000)).toEqual({ from: 10_000_000_000 - 3 * 3_600_000, to: 10_000_000_000 });
    expect(parseRange({ from: "0", to: String(48 * 3_600_000) }, 0)).toEqual({ from: 24 * 3_600_000, to: 48 * 3_600_000 });
    expect(parseRange({ from: "1000", to: "2000" })).toEqual({ from: 1000, to: 2000 });
    expect(() => parseRange({ from: "abc" })).toThrow(/from/);
    expect(() => parseRange({ from: "5", to: "4" })).toThrow(/<=/);
    expect(() => parseRange({ to: "-1" })).toThrow(/to/);
  });
});

describe("REST", () => {
  it("health, state, settings", async () => {
    const { app, engine } = setup();
    cleanup = () => app.close();
    const h = await app.inject({ method: "GET", url: "/api/health" });
    expect(h.statusCode).toBe(200);
    expect(h.json()).toMatchObject({ ok: true });
    expect(typeof h.json().serverTime).toBe("number");

    const s = await app.inject({ method: "GET", url: "/api/state" });
    expect(s.json()).toMatchObject({ settings: engine.settings, lastTick: { price: 67000 } });

    expect((await app.inject({ method: "GET", url: "/api/settings" })).json()).toEqual(engine.settings);
    const put = await app.inject({ method: "PUT", url: "/api/settings", payload: { volLookbackMin: 45 } });
    expect(put.statusCode).toBe(200);
    expect(put.json().volLookbackMin).toBe(45);
    const bad = await app.inject({ method: "PUT", url: "/api/settings", payload: { volLookbackMin: "x" } });
    expect(bad.statusCode).toBe(400);
    expect(bad.json().error).toBeTruthy();
    const arr = await app.inject({ method: "PUT", url: "/api/settings", payload: [1] });
    expect(arr.statusCode).toBe(400);
  });

  it("history routes: default range, explicit range, validation, 24h cap", async () => {
    const { app } = setup();
    cleanup = () => app.close();
    const bars = (await app.inject({ method: "GET", url: "/api/history/bars" })).json();
    expect(bars).toHaveLength(2); // seeded 60s (2h ago) + live 1s; 30h-old bar excluded
    expect(bars[0].d).toBe(60);
    expect(bars[0].t).toBeLessThan(bars[1].t);

    const capped = (await app.inject({ method: "GET", url: `/api/history/bars?from=0&to=${NOW}` })).json();
    expect(capped.every((b: { t: number }) => b.t >= NOW - MAX_RANGE_MS)).toBe(true);

    const narrow = await app.inject({ method: "GET", url: `/api/history/bars?from=${NOW - 10_000}&to=${NOW}` });
    expect(narrow.json()).toHaveLength(1);

    expect((await app.inject({ method: "GET", url: "/api/history/bars?from=nope" })).statusCode).toBe(400);
    expect((await app.inject({ method: "GET", url: `/api/history/odds?from=${NOW}&to=${NOW - 1}` })).statusCode).toBe(400);

    const odds = (await app.inject({ method: "GET", url: "/api/history/odds" })).json();
    expect(odds).toEqual([{ ts: NOW - 1000, windowId: "w", upBid: 40, upAsk: 42, downBid: 58, downAsk: 60, upMid: 41 }]);
    const fair = (await app.inject({ method: "GET", url: "/api/history/fair" })).json();
    expect(fair[0]).toMatchObject({ fair: 45, tLeftSec: 300, edge: 4 });
  });

  it("windows newest first with limit", async () => {
    const { app } = setup();
    cleanup = () => app.close();
    const w = (await app.inject({ method: "GET", url: "/api/windows?limit=3" })).json();
    expect(w.map((x: { id: string }) => x.id)).toEqual(["w0", "w1", "w2"]);
    expect((await app.inject({ method: "GET", url: "/api/windows" })).json()).toHaveLength(5);
    expect((await app.inject({ method: "GET", url: "/api/windows?limit=0" })).statusCode).toBe(400);
  });

  it("CORS headers", async () => {
    const { app } = setup(["http://localhost:3000"]);
    cleanup = () => app.close();
    const ok = await app.inject({ method: "GET", url: "/api/health", headers: { origin: "http://localhost:3000" } });
    expect(ok.headers["access-control-allow-origin"]).toBe("http://localhost:3000");
    const no = await app.inject({ method: "GET", url: "/api/health", headers: { origin: "http://evil.test" } });
    expect(no.headers["access-control-allow-origin"]).toBeUndefined();
  });
});

describe("WebSocket /ws", () => {
  async function listen(corsOrigin: string[] | "*" = "*") {
    const ctx = setup(corsOrigin);
    await ctx.app.listen({ port: 0, host: "127.0.0.1" });
    const port = (ctx.app.server.address() as AddressInfo).port;
    cleanup = () => ctx.app.close();
    return { ...ctx, port };
  }

  function collect(ws: WebSocket) {
    const msgs: ServerMsg[] = [];
    ws.on("message", (d) => msgs.push(JSON.parse(String(d))));
    const waitFor = async (pred: (m: ServerMsg) => boolean, ms = 2000) => {
      const start = Date.now();
      while (Date.now() - start < ms) {
        const m = msgs.find(pred);
        if (m) return m;
        await new Promise((r) => setTimeout(r, 10));
      }
      throw new Error("timeout waiting for message");
    };
    return { msgs, waitFor };
  }

  it("sends snapshot first, then engine messages and periodic feeds", async () => {
    const { port, engine, app } = await listen();
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    const { msgs, waitFor } = collect(ws);
    await new Promise((r) => ws.once("open", r));
    await waitFor((m) => m.type === "snapshot");
    expect(msgs[0]!.type).toBe("snapshot");
    expect((msgs[0] as Extract<ServerMsg, { type: "snapshot" }>).data.lastTick?.price).toBe(67000);
    expect(app.wsHub.clientCount()).toBe(1);

    engine.emit("msg", { type: "btc", data: { ts: 1, price: 2, sources: {} } } satisfies ServerMsg);
    expect(await waitFor((m) => m.type === "btc")).toMatchObject({ data: { price: 2 } });
    const feeds = await waitFor((m) => m.type === "feeds");
    expect((feeds as Extract<ServerMsg, { type: "feeds" }>).data).toHaveLength(2);

    await app.inject({ method: "PUT", url: "/api/settings", payload: { mode: "paper", feeCentsPerContract: 1 } });
    expect(await waitFor((m) => m.type === "settings")).toMatchObject({ data: { feeCentsPerContract: 1 } });

    ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    ws.close();
  });

  it("rejects other paths and disallowed origins", async () => {
    const { port } = await listen(["http://ok.test"]);
    const errFor = (url: string, origin?: string) =>
      new Promise<string>((resolve) => {
        const ws = new WebSocket(url, origin ? { origin } : {});
        ws.on("unexpected-response", (_req, res) => resolve(String(res.statusCode)));
        ws.on("error", (e) => resolve(e.message));
        ws.on("open", () => resolve("open"));
      });
    expect(await errFor(`ws://127.0.0.1:${port}/nope`)).not.toBe("open");
    expect(await errFor(`ws://127.0.0.1:${port}/ws`, "http://evil.test")).not.toBe("open");
    expect(await errFor(`ws://127.0.0.1:${port}/ws`, "http://ok.test")).toBe("open");
  });

  it("app.close() terminates clients and detaches from the engine", async () => {
    const { port, app, engine } = await listen();
    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws`);
    await new Promise((r) => ws.once("open", r));
    const closed = new Promise((r) => ws.once("close", r));
    expect(engine.listenerCount("msg")).toBe(1);
    await app.close();
    cleanup = null;
    await closed;
    expect(engine.listenerCount("msg")).toBe(0);
  });
});
