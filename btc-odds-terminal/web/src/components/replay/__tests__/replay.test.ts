// Replay (step 6), web side: the progressive feed (in-place appends vs rebuilds, tick-built partial candles),
// frames, and the controller in lib/replay.ts against a stubbed REST backend (start, seek, playback, the series
// guard and the hand-back to live). Run from web/:
//   NODE_OPTIONS=--conditions=import ../backend/node_modules/.bin/tsx --test src/components/replay/__tests__/*.test.ts

import { afterEach, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import type { Bar, BtcTick, FairSnap, MarketWindow, OddsSnap, ServerMsg } from "@shared/protocol";
import { ReplayFeed, TICK_FRESH_MS } from "../feed";
import { buildFrames, frameIndexAt } from "../frames";
import { EMPTY_LIVE_MARKET, mergeTicks, reduceLiveMarket } from "../live-tap";
import {
  beginReplayScrub,
  endReplayScrub,
  fallbackMarket,
  liveMarket,
  pauseReplay,
  playReplay,
  playableEnd,
  seekReplay,
  setLiveSource,
  setReplaySpeed,
  startReplay,
  stopReplay,
  toggleReplayPlaying,
  useReplayInfo,
  type ReplayPayload,
} from "../../../lib/replay";
import { selectOdds, useTerminal } from "../../../lib/store";
import { resetStore, snapshot, win } from "../../../lib/__tests__/helpers";

const MIN = 60_000;
const T0 = Date.UTC(2026, 9, 20, 14, 30, 0);
const W: MarketWindow = win(T0, "settled", { priceToBeat: 67_000, result: "up", settlePrice: 67_040 });

const bar = (t: number, c: number, d = 1): Bar => ({ t, d, o: c - 1, h: c + 1, l: c - 2, c });
const odds = (ts: number, mid = 50): OddsSnap => ({ ts, windowId: W.id, upBid: mid - 1, upAsk: mid + 1, downBid: 49 - mid + 50, downAsk: 51 - mid + 50, upMid: mid });
const fairS = (ts: number, f = 55): FairSnap => ({ ts, windowId: W.id, fair: f, sigma: 1e-4, tLeftSec: 300, edge: null });
const tick = (ts: number, price: number): BtcTick => ({ ts, price, sources: {} });

function payload(withTicks = true): ReplayPayload {
  const bars: Bar[] = [];
  const ticks: BtcTick[] = [];
  const os: OddsSnap[] = [];
  const fs: FairSnap[] = [];
  bars.push(bar(T0 - 5 * MIN, 66_900, 60)); // a seeded minute bar in the lead-in
  for (let ts = T0 - 4 * MIN; ts < W.endTs; ts += 1000) {
    const px = 67_000 + (ts - T0) / 1000;
    bars.push(bar(ts, px));
    ticks.push(tick(ts + 200, px - 0.5), tick(ts + 700, px));
    if (ts >= T0) {
      os.push(odds(ts, 40 + ((ts - T0) / 1000) * 0.02));
      fs.push(fairS(ts));
    }
  }
  return { window: W, bars, odds: os, fair: fs, frames: [], fills: [], ...(withTicks ? { ticks } : {}) };
}

// ---- feed -----------------------------------------------------------------------------------------------

test("feed: forward seeks append in place, backward seeks rebuild with new arrays", () => {
  const p = payload();
  const feed = new ReplayFeed({ bars: p.bars, ticks: p.ticks!, odds: p.odds, fair: p.fair });
  assert.equal(feed.seek(T0), "rebuild");
  const arrays = feed.out;
  const barsAtStart = feed.out.bars.length;
  // bars closed by T0: the seeded minute + 240 lead seconds (bar [T0-1s, T0) closes at T0)
  assert.equal(barsAtStart, 1 + 240);
  assert.equal(feed.out.odds.length, 1);
  assert.equal(feed.seek(T0 + 10_000), "append");
  assert.equal(feed.out, arrays);
  assert.equal(feed.out.bars, arrays.bars);
  assert.equal(feed.out.odds.length, 11);
  assert.ok(feed.out.bars.every((b, i) => i === 0 || b.t > feed.out.bars[i - 1].t));
  assert.equal(feed.seek(T0 + 10_000), "none");
  assert.equal(feed.seek(T0 + 5_000), "rebuild");
  assert.notEqual(feed.out.bars, arrays.bars);
  assert.equal(feed.out.odds.length, 6);
  assert.ok(feed.out.odds.every((o) => o.ts <= T0 + 5_000));
});

test("feed: with ticks the in-progress 1s bar forms from the ticks seen so far", () => {
  const p = payload();
  const feed = new ReplayFeed({ bars: p.bars, ticks: p.ticks!, odds: p.odds, fair: p.fair });
  feed.seek(T0 + 300); // one tick (T0+200) inside bar [T0, T0+1s)
  let last = feed.out.bars[feed.out.bars.length - 1];
  assert.equal(last.t, T0);
  assert.equal(last.c, 67_000 - 0.5);
  assert.equal(feed.latest().tick?.ts, T0 + 200);
  const n = feed.out.bars.length;
  feed.seek(T0 + 800); // second tick: same bar, replaced (not duplicated)
  last = feed.out.bars[feed.out.bars.length - 1];
  assert.equal(feed.out.bars.length, n);
  assert.equal(last.c, 67_000);
  feed.seek(T0 + 1000); // bar closed: the recorded bar replaces the partial
  assert.equal(feed.out.bars[n - 1], p.bars.find((b) => b.t === T0));
});

test("feed: without ticks only closed seconds show and lastTick comes from the latest close", () => {
  const p = payload(false);
  const feed = new ReplayFeed({ bars: p.bars, ticks: [], odds: p.odds, fair: p.fair });
  feed.seek(T0 + 1500);
  const last = feed.out.bars[feed.out.bars.length - 1];
  assert.equal(last.t, T0); // [T0, T0+1s) closed; [T0+1s, T0+2s) not yet
  const l = feed.latest();
  assert.equal(l.tick?.price, last.c);
  assert.equal(l.tick?.ts, T0 + 1000);
  assert.equal(l.odds?.ts, T0 + 1000);
  assert.equal(l.fair?.ts, T0 + 1000);
  assert.ok(TICK_FRESH_MS > 0);
});

test("frames: per second over the range, index lookup", () => {
  const p = payload(false);
  const f = buildFrames(p.bars, p.odds, p.fair, T0, T0 + 10_000);
  assert.equal(f.length, 11);
  assert.equal(f[0].upMid, 40);
  assert.equal(f[0].fair, 55);
  assert.equal(f[0].edge, 15);
  assert.equal(f[5].btc, 67_004);
  assert.equal(frameIndexAt(f, T0 + 4_500), 4);
  assert.equal(frameIndexAt(f, T0 - 1), -1);
});

test("playableEnd: whole window once ended, recording end while live", () => {
  const p = payload(false);
  assert.equal(playableEnd(p, W.endTs + 1), W.endTs);
  const live = { ...p, window: { ...W, status: "live" as const }, bars: p.bars.filter((b) => b.t < T0 + 60_000), odds: p.odds.filter((o) => o.ts < T0 + 60_000), fair: [] };
  assert.equal(playableEnd(live, T0 + 90_000), T0 + 60_000);
  assert.equal(playableEnd({ ...live, bars: [], odds: [] }, T0 + 90_000), T0);
});

// ---- controller ------------------------------------------------------------------------------------------

const realFetch = globalThis.fetch;
let routes: Record<string, () => unknown>;
let fetched: string[];
/** the live tap's stream (a stand-in for the second socket) */
let liveEmit: ((m: ServerMsg) => void) | null = null;

beforeEach(() => {
  resetStore();
  liveEmit = null;
  setLiveSource((emit) => {
    liveEmit = emit;
    return () => {
      if (liveEmit === emit) liveEmit = null;
    };
  });
  useTerminal.setState({ current: win(W.endTs + 30 * MIN, "live"), recent: [W] });
  fetched = [];
  routes = {};
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    fetched.push(url);
    const path = new URL(url).pathname;
    const route = Object.keys(routes).find((k) => path.endsWith(k));
    if (!route) return new Response(JSON.stringify({ error: "nope", code: "not_found" }), { status: 404 });
    return new Response(JSON.stringify(routes[route]()), { status: 200 });
  }) as typeof fetch;
});

afterEach(async () => {
  pauseReplay();
  if (useTerminal.getState().replay.active) {
    routes["/api/state"] ??= () => snapshot(Date.now());
    routes["/history/bars"] ??= () => [];
    routes["/history/odds"] ??= () => [];
    routes["/history/fair"] ??= () => [];
    await stopReplay();
  }
  globalThis.fetch = realFetch;
});

test("controller: start installs the window at its start and plays; seeks reuse / replace arrays", async () => {
  routes[`/api/replay/${W.id}`] = () => payload();
  const liveTicks = [tick(Date.now(), 1)];
  useTerminal.getState().setHistory({ ticks: liveTicks });
  const p = startReplay(W.id);
  let st = useTerminal.getState();
  assert.equal(st.replay.active, true);
  assert.equal(st.replay.loading, true);
  assert.equal(st.series.bars.length, 0, "chart blanked while loading");
  await p;
  pauseReplay();
  st = useTerminal.getState();
  assert.equal(st.replay.loading, false);
  assert.equal(st.current?.id, W.id);
  assert.equal(st.next, null);
  assert.equal(st.replay.startTs, T0);
  assert.equal(st.replay.endTs, W.endTs);
  assert.ok(st.replay.cursorTs! >= T0 && st.replay.cursorTs! < T0 + 5_000);
  assert.ok(st.series.bars.every((b) => b.t < st.replay.cursorTs!));
  assert.equal(useReplayInfo.getState().frames.length, 901, "frames built client-side when the payload has none");
  assert.equal(useReplayInfo.getState().hasTicks, true);

  const bars = st.series.bars;
  const v = st.version;
  seekReplay(T0 + 120_000);
  st = useTerminal.getState();
  assert.equal(st.series.bars, bars, "forward seek mutates in place");
  assert.ok(st.version > v);
  assert.equal(st.replay.cursorTs, T0 + 120_000);
  assert.equal(st.lastOdds?.ts, T0 + 120_000);
  assert.ok(st.lastTick!.ts <= T0 + 120_000);

  seekReplay(T0 + 30_000);
  st = useTerminal.getState();
  assert.notEqual(st.series.bars, bars, "backward seek installs new arrays");
  assert.ok(st.series.odds.every((o) => o.ts <= T0 + 30_000));

  seekReplay(W.endTs + 99 * MIN); // clamped
  assert.equal(useTerminal.getState().replay.cursorTs, W.endTs);
});

test("controller: playback advances the cursor at the chosen speed and stops at the end", async () => {
  routes[`/api/replay/${W.id}`] = () => payload(false);
  await startReplay(W.id);
  setReplaySpeed(16);
  const c0 = useTerminal.getState().replay.cursorTs!;
  await new Promise((r) => setTimeout(r, 300));
  const c1 = useTerminal.getState().replay.cursorTs!;
  assert.ok(c1 - c0 >= 16 * 150, `16x moved ${c1 - c0}ms in 300ms`);
  assert.equal(useTerminal.getState().replay.speed, 16);

  seekReplay(W.endTs - 100);
  await new Promise((r) => setTimeout(r, 120));
  let r = useTerminal.getState().replay;
  assert.equal(r.cursorTs, W.endTs);
  assert.equal(r.playing, false);
  toggleReplayPlaying(); // at the end: restart from the start
  r = useTerminal.getState().replay;
  assert.equal(r.playing, true);
  assert.ok(r.cursorTs! < T0 + 2_000);
  pauseReplay();
  assert.equal(useTerminal.getState().replay.playing, false);
  playReplay();
  assert.equal(useTerminal.getState().replay.playing, true);
});

test("controller: a live history load during a replay is replaced by the replay series", async () => {
  routes[`/api/replay/${W.id}`] = () => payload();
  await startReplay(W.id);
  pauseReplay();
  seekReplay(T0 + 60_000);
  const before = useTerminal.getState().series.odds.length;
  useTerminal.getState().setHistory({ bars: [bar(Date.now(), 1)], odds: [], fair: [] }); // e.g. ws reconnect
  const st = useTerminal.getState();
  assert.equal(st.series.odds.length, before);
  assert.ok(st.series.bars.length > 1);
  assert.ok(st.series.bars.every((b) => b.t < T0 + 61_000));
});

test("controller: exit restores live state, history and the pre-replay ticks", async () => {
  routes[`/api/replay/${W.id}`] = () => payload();
  const liveTicks = [tick(Date.now() - 1000, 70_000)];
  useTerminal.getState().setHistory({ ticks: liveTicks });
  await startReplay(W.id);
  const liveWin = win(Math.floor(Date.now() / (15 * MIN)) * 15 * MIN, "live");
  const liveBars = [bar(Date.now() - 2000, 70_000), bar(Date.now() - 1000, 70_001)];
  routes["/api/state"] = () => snapshot(Date.now(), { current: liveWin, recent: [W] });
  routes["/history/bars"] = () => liveBars;
  routes["/history/odds"] = () => [];
  routes["/history/fair"] = () => [];
  const p = stopReplay();
  assert.equal(useTerminal.getState().replay.active, true, "muted until the live state is in hand");
  await p;
  const st = useTerminal.getState();
  assert.equal(st.replay.active, false);
  assert.equal(st.current?.id, liveWin.id);
  assert.deepEqual(st.series.bars, liveBars);
  assert.deepEqual(st.series.ticks, liveTicks);
  assert.equal(useReplayInfo.getState().window, null);
  assert.ok(fetched.some((u) => u.includes("/api/state")));
});

test("controller: load errors keep the replay open with the error (retry / exit)", async () => {
  await startReplay("missing-window");
  const r = useTerminal.getState().replay;
  assert.equal(r.active, true);
  assert.equal(r.loading, false);
  assert.equal(r.error, "Window not recorded");
});

// ---- live market during a replay ----------------------------------------------------------------------------

test("live tap: reduceLiveMarket follows windows, rollovers and quotes like ingest", () => {
  const A = win(T0, "live");
  const B = win(T0 + 15 * MIN, "upcoming");
  let m = reduceLiveMarket(EMPTY_LIVE_MARKET, { type: "window", data: A });
  assert.equal(m.current?.id, A.id);
  m = reduceLiveMarket(m, { type: "window", data: B });
  assert.equal(m.next?.id, B.id);
  m = reduceLiveMarket(m, { type: "odds", data: odds(T0 + 1000) });
  assert.equal(m.lastOdds?.ts, T0 + 1000);
  const book = { ts: T0, windowId: A.id, instrument: A.upInstrument, bids: [], asks: [], state: "open" } as never;
  m = reduceLiveMarket(m, { type: "book", data: book });
  const live = { ...B, status: "live" as const };
  m = reduceLiveMarket(m, { type: "rollover", data: { current: live, next: null } as never });
  assert.equal(m.current?.id, B.id);
  assert.equal(m.next, null);
  assert.equal(m.book, null, "the old window's book is dropped on rollover");
  assert.equal(reduceLiveMarket(m, { type: "feeds", data: [] }), m, "unrelated messages keep the reference");
});

test("live tap: mergeTicks appends the buffered ticks (buffer wins on overlap) and caps the span", () => {
  const saved = [tick(1000, 1), tick(2000, 2), tick(3000, 3)];
  const buf = [tick(3000, 30), tick(4000, 4)];
  assert.deepEqual(mergeTicks(saved, buf, 10_000).map((t) => t.price), [1, 2, 30, 4]);
  assert.deepEqual(mergeTicks(saved, buf, 1_500).map((t) => t.ts), [3000, 4000]);
  assert.deepEqual(mergeTicks(saved, [], 10_000), saved);
});

test("controller: liveMarket() keeps the live window and quotes while the store shows the replay", async () => {
  const liveWin = useTerminal.getState().current!;
  const liveOdds: OddsSnap = { ...odds(Date.now(), 30), windowId: liveWin.id };
  useTerminal.setState({ lastOdds: liveOdds, lastTick: tick(Date.now(), 70_000) });
  routes[`/api/replay/${W.id}`] = () => payload();
  await startReplay(W.id);
  pauseReplay();
  seekReplay(T0 + 60_000);
  const st = useTerminal.getState();
  assert.equal(st.current?.id, W.id, "the store follows the replay");
  assert.equal(liveMarket().current?.id, liveWin.id, "the live view does not");
  assert.equal(selectOdds(liveMarket())?.upBid, liveOdds.upBid);
  assert.equal(liveMarket().lastTick?.price, 70_000);
  // live messages muted by ingest reach the tap
  assert.ok(liveEmit, "the live tap subscribed");
  const newer: OddsSnap = { ...liveOdds, ts: liveOdds.ts + 1000, upBid: 31 };
  liveEmit!({ type: "odds", data: newer });
  liveEmit!({ type: "btc", data: tick(Date.now() + 500, 70_010) });
  assert.equal(selectOdds(liveMarket())?.upBid, 31);
  assert.equal(liveMarket().lastTick?.price, 70_010);
  assert.equal(useTerminal.getState().lastOdds?.windowId, W.id, "the replay's quotes are untouched");
});

test("controller: exit merges the live ticks muted during the replay into the tick history", async () => {
  routes[`/api/replay/${W.id}`] = () => payload();
  const before = [tick(Date.now() - 2000, 70_000)];
  useTerminal.getState().setHistory({ ticks: before });
  await startReplay(W.id);
  pauseReplay();
  liveEmit!({ type: "btc", data: tick(Date.now() - 500, 70_005) });
  liveEmit!({ type: "btc", data: tick(Date.now() - 100, 70_006) });
  routes["/api/state"] = () => snapshot(Date.now(), { current: win(Math.floor(Date.now() / (15 * MIN)) * 15 * MIN, "live") });
  routes["/history/bars"] = () => [];
  routes["/history/odds"] = () => [];
  routes["/history/fair"] = () => [];
  await stopReplay();
  assert.deepEqual(useTerminal.getState().series.ticks.map((t) => t.price), [70_000, 70_005, 70_006]);
  assert.equal(liveEmit, null, "the tap is closed on exit");
});

test("controller: a failed exit snapshot installs the live tap's window, then heals in the background", async () => {
  routes[`/api/replay/${W.id}`] = () => payload();
  await startReplay(W.id);
  pauseReplay();
  const liveWin = win(Math.floor(Date.now() / (15 * MIN)) * 15 * MIN, "live");
  liveEmit!({ type: "window", data: liveWin });
  routes["/history/bars"] = () => [];
  routes["/history/odds"] = () => [];
  routes["/history/fair"] = () => [];
  // no /api/state route: every attempt fails (404)
  await stopReplay();
  let st = useTerminal.getState();
  assert.equal(st.replay.active, false);
  assert.equal(st.current?.id, liveWin.id, "never the replayed window");
  assert.notEqual(st.lastOdds?.windowId, W.id);
  assert.ok(fetched.filter((u) => u.includes("/api/state")).length >= 3, "the exit snapshot was retried");
  const healed = win(liveWin.startTs, "live", { priceToBeat: 71_000 });
  routes["/api/state"] = () => snapshot(Date.now(), { current: healed });
  await new Promise((r) => setTimeout(r, 1300));
  st = useTerminal.getState();
  assert.equal(st.current?.priceToBeat, 71_000, "the background retry applied the snapshot");
});

test("fallbackMarket: a tap whose window already ended clears the window-bound fields", () => {
  const ended = win(T0, "live");
  const m = fallbackMarket({ ...EMPTY_LIVE_MARKET, current: ended, lastOdds: odds(T0), lastTick: tick(T0, 1) }, ended.endTs + 1);
  assert.equal(m.current, null);
  assert.equal(m.lastOdds, null);
  assert.equal(m.lastTick?.price, 1);
  assert.equal(fallbackMarket({ ...EMPTY_LIVE_MARKET, current: ended }, ended.endTs - 1).current, ended);
});

test("controller: releasing a scrub at the end stays at the end (no restart)", async () => {
  routes[`/api/replay/${W.id}`] = () => payload(false);
  await startReplay(W.id);
  assert.equal(useTerminal.getState().replay.playing, true);
  beginReplayScrub();
  seekReplay(W.endTs + MIN);
  endReplayScrub();
  let r = useTerminal.getState().replay;
  assert.equal(r.cursorTs, W.endTs);
  assert.equal(r.playing, false);
  // a scrub released mid-window resumes playback
  playReplay(); // restart from the start
  beginReplayScrub();
  seekReplay(T0 + 60_000);
  endReplayScrub();
  r = useTerminal.getState().replay;
  assert.equal(r.playing, true);
  assert.equal(r.cursorTs, T0 + 60_000);
});
