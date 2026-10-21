// Unit tests for the shell data layer (no DOM). Run from web/:
//   ../backend/node_modules/.bin/tsx --test src/lib/__tests__/*.test.ts

import { afterEach, beforeEach, mock, test } from "node:test";
import assert from "node:assert/strict";
import type { ServerMsg } from "@shared/protocol";
import { STABLE_MS, STALL_MS, startReal } from "../ws";
import { useTerminal } from "../store";
import { resetStore, snapshot } from "./helpers";

/** Minimal browser-like WebSocket double. */
class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  static all: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  closeCalls = 0;
  sent: string[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.closeCalls++;
    this.readyState = FakeSocket.CLOSED;
  }
  // test drivers
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.({});
  }
  msg(m: ServerMsg) {
    this.onmessage?.({ data: JSON.stringify(m) });
  }
  serverClose() {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({});
  }
}

const SERVER_AHEAD = -5 * 60_000; // client clock is 5 min AHEAD of the server
const realWS = globalThis.WebSocket;
const realFetch = globalThis.fetch;
let fetches: string[] = [];
let fetchImpl: (url: string) => Promise<Response>;
let stop: (() => void) | null = null;

const json = (v: unknown) => Promise.resolve(new Response(JSON.stringify(v), { status: 200 }));
/** let resolved promises (fetch, res.json) settle */
async function settle() {
  for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
}
const last = () => FakeSocket.all[FakeSocket.all.length - 1];

beforeEach(() => {
  resetStore();
  FakeSocket.all = [];
  fetches = [];
  fetchImpl = () => json([]);
  (globalThis as { WebSocket: unknown }).WebSocket = FakeSocket;
  globalThis.fetch = ((url: string) => {
    fetches.push(String(url));
    return fetchImpl(String(url));
  }) as typeof fetch;
  mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now: 1_700_000_000_000 });
  mock.method(Math, "random", () => 0.5); // jitter factor exactly 1
});

afterEach(() => {
  stop?.();
  stop = null;
  mock.timers.reset();
  mock.restoreAll();
  globalThis.WebSocket = realWS;
  globalThis.fetch = realFetch;
});

test("connected and history wait for the snapshot; history range and cutoff use server time", async () => {
  stop = startReal();
  const s = last();
  s.open();
  await settle();
  assert.equal(useTerminal.getState().connected, false);
  assert.equal(fetches.length, 0, "no history before the snapshot");
  const serverTime = Date.now() + SERVER_AHEAD;
  s.msg({ type: "snapshot", data: snapshot(serverTime) });
  await settle();
  assert.equal(useTerminal.getState().connected, true);
  assert.equal(fetches.length, 3);
  const to = Number(new URL(fetches[0]).searchParams.get("to"));
  assert.equal(to, serverTime + 60_000, "`to` is server now + 60s, not the client clock");
});

test("live bars streamed during the history fetch survive the merge when the client clock is ahead", async () => {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const serverTime = Date.now() + SERVER_AHEAD;
  fetchImpl = async (url) => {
    await gate;
    return url.includes("/bars") ? json([{ t: serverTime - 60_000, d: 60, o: 1, h: 1, l: 1, c: 1 }]) : json([]);
  };
  stop = startReal();
  const s = last();
  s.open();
  s.msg({ type: "snapshot", data: snapshot(serverTime) });
  s.msg({ type: "btc", data: { ts: serverTime + 500, price: 2, sources: {} } });
  release();
  await settle();
  const bars = useTerminal.getState().series.bars;
  assert.deepEqual(
    bars.map((b) => b.t),
    [serverTime - 60_000, Math.floor((serverTime + 500) / 1000) * 1000],
  );
});

test("watchdog: a silent (half-open) socket is dropped and reconnected", async () => {
  stop = startReal();
  const s = last();
  s.open();
  s.msg({ type: "snapshot", data: snapshot(Date.now()) });
  await settle();
  assert.equal(useTerminal.getState().connected, true);
  // feeds keep it alive
  for (let i = 0; i < 10; i++) {
    mock.timers.tick(1000);
    s.msg({ type: "feeds", data: [] });
  }
  assert.equal(s.closeCalls, 0);
  assert.equal(FakeSocket.all.length, 1);
  // then nothing arrives, socket still "OPEN"
  mock.timers.tick(STALL_MS + 1000);
  assert.equal(s.closeCalls, 1);
  assert.equal(useTerminal.getState().connected, false);
  mock.timers.tick(STABLE_MS + 15_000);
  assert.equal(FakeSocket.all.length, 2, "a new socket was opened");
});

test("backoff keeps growing when the server accepts and immediately closes", () => {
  stop = startReal();
  const delays: number[] = [];
  for (let i = 0; i < 6; i++) {
    const s = last();
    s.open();
    s.serverClose();
    const n = FakeSocket.all.length;
    let waited = 0;
    while (FakeSocket.all.length === n) {
      mock.timers.tick(100);
      waited += 100;
    }
    delays.push(waited);
  }
  for (let i = 1; i < delays.length; i++) assert.ok(delays[i] > delays[i - 1], `delays grow: ${delays}`);
});

test("backoff resets once a connection has been stable after its snapshot", () => {
  stop = startReal();
  for (let i = 0; i < 4; i++) {
    last().serverClose();
    mock.timers.tick(20_000);
  }
  const s = last();
  s.open();
  s.msg({ type: "snapshot", data: snapshot(Date.now()) });
  for (let i = 0; i < (STABLE_MS + 1000) / 1000; i++) {
    mock.timers.tick(1000);
    s.msg({ type: "feeds", data: [] });
  }
  const n = FakeSocket.all.length;
  s.serverClose();
  mock.timers.tick(700); // BACKOFF_MIN (500) after a reset
  assert.equal(FakeSocket.all.length, n + 1);
});

test("history: a failed series is retried until it loads", async () => {
  let barsFails = 2;
  fetchImpl = (url) => {
    if (url.includes("/bars") && barsFails-- > 0) return Promise.resolve(new Response("busy", { status: 503 }));
    return url.includes("/bars") ? json([{ t: 1, d: 60, o: 1, h: 1, l: 1, c: 1 }]) : json([]);
  };
  stop = startReal();
  const s = last();
  s.open();
  s.msg({ type: "snapshot", data: snapshot(Date.now()) });
  await settle();
  assert.equal(fetches.length, 3);
  assert.equal(useTerminal.getState().series.bars.length, 0);
  for (let i = 0; i < 4; i++) {
    mock.timers.tick(1000);
    s.msg({ type: "feeds", data: [] });
    await settle();
  }
  assert.equal(fetches.filter((u) => u.includes("/odds")).length, 1, "loaded series are not re-fetched");
  assert.equal(fetches.filter((u) => u.includes("/bars")).length, 3);
  assert.equal(useTerminal.getState().series.bars[0]?.t, 1);
});

test("history: a hung request is aborted after the timeout and retried", async () => {
  let hang = true;
  fetchImpl = (url) => {
    if (url.includes("/fair") && hang) {
      hang = false;
      return new Promise(() => {}); // never resolves on its own...
    }
    return json([]);
  };
  // ...but our fetch double honours the abort signal like the real one
  const inner = globalThis.fetch;
  globalThis.fetch = ((url: string, init?: RequestInit) =>
    new Promise((resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
      inner(url, init).then(resolve, reject);
    })) as typeof fetch;
  stop = startReal();
  const s = last();
  s.open();
  s.msg({ type: "snapshot", data: snapshot(Date.now()) });
  await settle();
  for (let i = 0; i < 13; i++) {
    mock.timers.tick(1000);
    s.msg({ type: "feeds", data: [] });
    await settle();
  }
  assert.equal(fetches.filter((u) => u.includes("/fair")).length, 2);
});
