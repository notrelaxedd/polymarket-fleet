"use client";
// Replay controller: load a recorded window (api.replay), swap it into the store and play it at 1x/4x/16x.
// OWNER: replay.
//
// How the chart follows a replay (no chart special cases):
//  - start: the store's series are blanked, one frame later the window's data up to the cursor is installed
//    with NEW arrays (the chart rebuilds and frames the view as on a fresh load: previous window + this one);
//  - playback / forward scrubbing: ReplayFeed pushes into those arrays IN PLACE and `version` is bumped, the
//    same incremental path live ingest uses (the chart only re-folds its last bucket);
//  - backward scrubbing installs new arrays again (full rebuild, the user's pan/zoom is kept);
//  - current / lastTick / lastOdds / lastFair are set from the cursor and serverNow() returns the cursor, so
//    the now line, countdown, PTB line, window bands and legend all show the replayed moment.
//
// Header decision: the header FOLLOWS THE REPLAY (price to beat, BTC, Up/Down quotes, fair · edge and the
// countdown show the replayed values at the cursor); the amber REPLAY badge (ReplayBar, also exported as
// <ReplayBadge/> for the header) marks it. Feed dots, results strip, settings, positions, orders, risk and
// alerts stay live: ingest keeps applying those messages, and trading is blocked while replay.active.
//
// LIVE MARKET DURING A REPLAY: because the store's current / lastTick / lastOdds / lastFair / book (and
// serverNow()) follow the cursor, code that acts on the REAL market (position marks and P&L, Close / Edit
// bracket validation, "is this position's window live", new alerts and their cross direction) must read
// liveMarket() / useLiveMarket() and liveNow() instead. While a replay runs they come from a live tap (the
// mock stream, or a second socket) that this module keeps; otherwise they are the store's own fields.
//
// Live ingest is suspended for market data while replay.active (lib/ingest.ts skips btc/odds/fair/window/
// rollover/book). Exit fetches a fresh snapshot + 3h history WHILE STILL MUTED (the snapshot is retried a few
// times), blanks the chart for one frame and then hands back to live in one store update, so no live tick can
// land in the replay arrays and the chart frames the live view like a fresh load. The live ticks muted during
// the replay (buffered by the tap) are merged back so the per-exchange overlay has no hole. If the snapshot
// still fails, the live tap's market state is installed instead (never the replayed window) and a background
// loop keeps retrying the snapshot; failed history loads are finished by ws.resyncLive().

import { create } from "zustand";
import type { BtcTick, Fill, MarketWindow, ReplayData, ReplayFrame, ServerMsg, StateSnapshot } from "@shared/protocol";
import { ReplayFeed, emptySeries, type ReplaySeries } from "@/components/replay/feed";
import { buildFrames } from "@/components/replay/frames";
import { EMPTY_LIVE_MARKET, mergeTicks, pickLiveMarket, reduceLiveMarket, type LiveMarket } from "@/components/replay/live-tap";
import { api } from "./api";
import { WS_URL, isMock } from "./env";
import { TICK_CAP_MS, takeLiveDirty } from "./ingest";
import { getMock } from "./mock";
import { REPLAY_IDLE, liveNow, useTerminal, type ReplaySpeed, type ReplayState, type TerminalState } from "./store";
import { resyncLive } from "./ws";

/** GET /api/replay/:windowId payload (ReplayData carries optional `ticks`). */
export type ReplayPayload = ReplayData;

/** quiet time after the last backward key seek before the chart is re-framed */
const REFRAME_DEBOUNCE_MS = 200;
/** playback tick (the cursor advances by wall-clock dt × speed, so throttled timers stay in sync) */
const STEP_MS = 50;
/** live history reinstalled on exit (same span as lib/ws.ts) */
const HISTORY_MS = 3 * 3600 * 1000;
/** a nextFrame() that rAF never answers (background tab) resolves after this long */
const FRAME_FALLBACK_MS = 250;
/** waits between the exit's snapshot attempts (still muted; then the live tap takes over) */
const EXIT_STATE_RETRY_MS = [400, 1200];
/** background snapshot retry after a failed exit */
const HEAL_MIN_MS = 1000;
const HEAL_MAX_MS = 15_000;
export const REPLAY_SPEEDS: ReplaySpeed[] = [1, 4, 16];

// ---- replay info for the transport bar (rarely changes; kept out of the terminal store) ----

export interface ReplayInfo {
  window: MarketWindow | null;
  /** per-second frames over [startTs, endTs] (scrubber sparkline) */
  frames: ReplayFrame[];
  /** my fills in the window, ascending (scrubber markers) */
  fills: Fill[];
  /** raw ticks available (candles form tick by tick; else one closed second at a time) */
  hasTicks: boolean;
  /** the window was still live when loaded: playback ends where the recording ends */
  partial: boolean;
  /** stopReplay() is fetching the live state (replay.loading is true meanwhile) */
  exiting: boolean;
}

const EMPTY_INFO: ReplayInfo = { window: null, frames: [], fills: [], hasTicks: false, partial: false, exiting: false };
export const useReplayInfo = create<ReplayInfo>()(() => EMPTY_INFO);

// ---- live market while replaying (see the header comment) ----

export type { LiveMarket };
/** Live market state fed by the live tap while a replay runs (stale outside a replay: use liveMarket()). */
export const useLiveTap = create<LiveMarket>()(() => EMPTY_LIVE_MARKET);

/** A live message stream: subscribe(emit) returns its unsubscribe. */
export type LiveSource = (emit: (m: ServerMsg) => void) => () => void;
let sourceOverride: LiveSource | null = null;
/** Replace the live tap's source (tests); null restores the default (mock stream / second socket). */
export function setLiveSource(src: LiveSource | null): void {
  sourceOverride = src;
}

let tapOff: (() => void) | null = null;
/** live ticks muted by ingest during the replay (merged back into the tick history on exit) */
let tapTicks: BtcTick[] = [];

/**
 * The REAL market right now, replay or not: current / next window, last tick, odds, fair and book.
 * selectOdds(liveMarket()) / selectFair(liveMarket()) give the live window's quotes. Pair with liveNow().
 */
export function liveMarket(): LiveMarket {
  const st = store();
  return st.replay.active ? useLiveTap.getState() : pickLiveMarket(st);
}

/**
 * Hook form of liveMarket(). `sel` must return a primitive or an existing reference (e.g. m => m.current,
 * selectOdds); wrap object-building selectors in useShallow.
 */
export function useLiveMarket<T>(sel: (m: LiveMarket) => T): T {
  const active = useTerminal((s) => s.replay.active);
  const fromStore = useTerminal((s) => sel(s));
  const fromTap = useLiveTap(sel);
  return active ? fromTap : fromStore;
}

function startTap(seed: LiveMarket): void {
  stopTap();
  tapTicks = [];
  useLiveTap.setState(pickLiveMarket(seed), true);
  const src = sourceOverride ?? (isMock() ? mockSource : socketSource);
  tapOff = src(onTapMsg);
}

function stopTap(): void {
  tapOff?.();
  tapOff = null;
}

function onTapMsg(m: ServerMsg): void {
  if (m.type === "btc") {
    tapTicks.push(m.data);
    if (tapTicks.length % 1000 === 0) tapTicks = mergeTicks([], tapTicks, TICK_CAP_MS);
  }
  const cur = useLiveTap.getState();
  const next = reduceLiveMarket(cur, m);
  if (next !== cur) useLiveTap.setState(next, true);
}

const mockSource: LiveSource = (emit) => getMock().subscribe(emit);

/** A second, read-only socket (the main one's market messages are muted by ingest while replaying). */
const socketSource: LiveSource = (emit) => {
  let stopped = false;
  let sock: WebSocket | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let delay = HEAL_MIN_MS;
  const schedule = () => {
    if (stopped || retry) return;
    retry = setTimeout(() => {
      retry = null;
      connect();
    }, delay);
    delay = Math.min(HEAL_MAX_MS, delay * 2);
  };
  const connect = () => {
    if (stopped || typeof WebSocket === "undefined") return;
    try {
      sock = new WebSocket(WS_URL);
    } catch {
      schedule();
      return;
    }
    sock.onmessage = (ev) => {
      delay = HEAL_MIN_MS;
      try {
        emit(JSON.parse(ev.data as string) as ServerMsg);
      } catch {}
    };
    sock.onclose = () => {
      sock = null;
      schedule();
    };
    sock.onerror = () => {
      try {
        sock?.close();
      } catch {}
    };
  };
  connect();
  return () => {
    stopped = true;
    if (retry) clearTimeout(retry);
    if (sock) {
      sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
      try {
        sock.close();
      } catch {}
    }
    sock = null;
  };
};

// ---- session ----

interface Session {
  windowId: string;
  data: ReplayPayload;
  feed: ReplayFeed;
  startTs: number;
  endTs: number;
}

let session: Session | null = null;
/** bumped by every start / stop: async continuations of a superseded call bail out */
let seq = 0;
/** live per-exchange ticks from before the replay (real mode has no tick history endpoint to reload them) */
let savedLiveTicks: BtcTick[] | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let lastWall = 0;
let unguard: (() => void) | null = null;
let preferredSpeed: ReplaySpeed = 1;
let resumeAfterScrub = false;
let scrubbing = false;
/** a backward seek left the chart's view shifted (see reframe()) */
let needsReframe = false;
let reframeTimer: ReturnType<typeof setTimeout> | null = null;
/** reframe() has blanked the series on purpose: the guard must not reinstall them */
let reframing = false;

const store = () => useTerminal.getState();
const wall = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(finish);
    setTimeout(finish, FRAME_FALLBACK_MS);
  });
}

/** The loaded replay payload (null when idle / loading). */
export function replayData(): ReplayPayload | null {
  return session?.data ?? null;
}

function lastTs<T>(arr: readonly T[], ts: (x: T) => number): number {
  return arr.length ? ts(arr[arr.length - 1]) : -Infinity;
}

/** Playable range: the whole window once it has ended, else up to the end of the recording. */
export function playableEnd(d: Pick<ReplayData, "window" | "bars" | "odds" | "fair">, now: number): number {
  const w = d.window;
  if (w.endTs <= now) return w.endTs;
  const rec = Math.max(
    lastTs(d.bars, (b) => b.t + b.d * 1000),
    lastTs(d.odds, (o) => o.ts),
    lastTs(d.fair, (f) => f.ts),
  );
  return Math.min(w.endTs, Math.max(w.startTs, Math.min(rec, now)));
}

/**
 * Enter replay for a window (or switch to another while replaying) and start playing it from its start.
 * Window history rows and the toolbar's "Replay window" button call this.
 */
export async function startReplay(windowId: string): Promise<void> {
  const token = ++seq;
  stopTimer();
  cancelReframe();
  const st = store();
  if (!st.replay.active) {
    savedLiveTicks = st.series.ticks;
    startTap(st); // before replay.active: liveMarket() never sees the replayed values
    installGuard();
  }
  session = null;
  useReplayInfo.setState(EMPTY_INFO, true);
  st.setReplay({ ...REPLAY_IDLE, active: true, loading: true, windowId, speed: preferredSpeed });
  // blank the chart while loading: the replay's first data then frames the view like a fresh load
  useTerminal.setState((s) => ({ series: emptySeries(), version: s.version + 1, hover: null, book: null }));

  let d: ReplayPayload;
  try {
    [d] = await Promise.all([api.replay(windowId) as Promise<ReplayPayload>, nextFrame()]);
  } catch (e) {
    if (token === seq) store().setReplay({ loading: false, error: errorText(e) });
    return;
  }
  if (token !== seq) return;

  const w = d.window;
  const startTs = w.startTs;
  const endTs = playableEnd(d, liveNow());
  const firstTs = d.bars[0]?.t ?? startTs;
  const ticks = d.ticks ?? (isMock() ? getMock().ticks(firstTs, endTs) : []);
  const frames = d.frames.length ? d.frames : buildFrames(d.bars, d.odds, d.fair, startTs, endTs);
  const src: ReplaySeries = { bars: d.bars, ticks, odds: d.odds, fair: d.fair };
  const feed = new ReplayFeed(src);
  feed.seek(startTs);
  session = { windowId, data: { ...d, ticks, frames }, feed, startTs, endTs };
  useReplayInfo.setState(
    {
      window: w,
      frames,
      fills: [...d.fills].sort((a, b) => a.ts - b.ts),
      hasTicks: ticks.length > 0,
      partial: endTs < w.endTs,
      exiting: false,
    },
    true,
  );
  const l = feed.latest();
  const playing = endTs > startTs;
  useTerminal.setState((s) => ({
    current: w,
    next: null,
    lastTick: l.tick,
    lastOdds: l.odds,
    lastFair: l.fair,
    series: feed.out,
    version: s.version + 1,
    replay: { ...s.replay, loading: false, error: null, startTs, endTs, cursorTs: startTs, playing },
  }));
  if (playing) startTimer();
}

/** Leave replay and bring the chart / header back to live (see the header comment for the sequence). */
export async function stopReplay(): Promise<void> {
  if (!store().replay.active) return;
  const token = ++seq;
  stopTimer();
  cancelReframe();
  session = null;
  // still active: live market data stays muted while the live state is fetched
  useReplayInfo.setState({ exiting: true });
  store().setReplay({ playing: false, loading: true, error: null });
  const to = liveNow() + 60_000;
  const from = to - HISTORY_MS - 60_000;
  const soft = <T,>(p: Promise<T>) => p.catch(() => null);
  const [snap, bars, odds, fair] = await Promise.all([
    stateWithRetry(token),
    soft(api.bars(from, to)),
    soft(api.odds(from, to)),
    soft(api.fair(from, to)),
  ]);
  if (token !== seq) return;
  useTerminal.setState((s) => ({ series: emptySeries(), version: s.version + 1, hover: null }));
  await nextFrame();
  if (token !== seq) return;

  const ticks = isMock()
    ? getMock().ticks(Math.max(from, to - TICK_CAP_MS), to)
    : mergeTicks(savedLiveTicks ?? [], tapTicks, TICK_CAP_MS);
  const tap = useLiveTap.getState();
  stopTap();
  tapTicks = [];
  savedLiveTicks = null;
  unguard?.();
  unguard = null;
  useReplayInfo.setState(EMPTY_INFO, true);
  store().setReplay(REPLAY_IDLE); // first: applySnapshot ignores market data while a replay is active
  if (snap) store().applySnapshot(snap);
  else useTerminal.setState(fallbackMarket(tap, liveNow())); // never leave the replayed window as current
  store().setHistory({ bars: bars ?? [], odds: odds ?? [], fair: fair ?? [], ticks });
  takeLiveDirty();
  if (!snap) void healLive(token);
  else if (!bars || !odds || !fair || snapshotStale(snap)) await resyncLive();
}

/** api.state() with a few quick retries (exit is still muted meanwhile). null = failed / superseded. */
async function stateWithRetry(token: number): Promise<StateSnapshot | null> {
  for (let i = 0; ; i++) {
    try {
      return await api.state();
    } catch {
      if (i >= EXIT_STATE_RETRY_MS.length || token !== seq) return null;
      await sleep(EXIT_STATE_RETRY_MS[i]);
      if (token !== seq) return null;
    }
  }
}

/**
 * Market state to install when the exit snapshot failed: the live tap's (kept current by its own stream),
 * with the window-bound fields cleared when its window has already ended (no stream reached it).
 */
export function fallbackMarket(tap: LiveMarket, now: number): LiveMarket {
  if (tap.current && tap.current.endTs > now) return pickLiveMarket(tap);
  return { ...EMPTY_LIVE_MARKET, lastTick: tap.lastTick };
}

/** After a failed exit: keep fetching the snapshot (backoff) until it lands, then reload history. */
async function healLive(token: number): Promise<void> {
  let delay = HEAL_MIN_MS;
  for (;;) {
    await sleep(delay);
    if (token !== seq || store().replay.active) return;
    try {
      const snap = await api.state();
      if (token !== seq || store().replay.active) return;
      store().applySnapshot(snap);
      break;
    } catch {
      delay = Math.min(HEAL_MAX_MS, delay * 2);
    }
  }
  await resyncLive();
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** The live window in the snapshot ended while we were fetching (a rollover may have been muted). */
function snapshotStale(s: StateSnapshot): boolean {
  return !!s.current && s.current.endTs <= liveNow();
}

// ---- transport ----

export function setReplaySpeed(speed: ReplaySpeed): void {
  preferredSpeed = speed;
  store().setReplay({ speed });
  lastWall = wall();
}

export function playReplay(): void {
  const r = store().replay;
  if (!session || !r.active || r.loading) return;
  if ((r.cursorTs ?? session.startTs) >= session.endTs) applyCursor(session.startTs);
  if (session.endTs <= session.startTs) return;
  store().setReplay({ playing: true });
  startTimer();
}

export function pauseReplay(): void {
  stopTimer();
  if (store().replay.playing) store().setReplay({ playing: false });
}

export function toggleReplayPlaying(): void {
  if (store().replay.playing) pauseReplay();
  else playReplay();
}

/** Move the cursor (clamped to the playable range). Keeps playing unless the end is reached. */
export function seekReplay(ts: number, reframeDelayMs = 0): void {
  if (!session || !store().replay.active || store().replay.loading) return;
  const t = Math.round(Math.min(session.endTs, Math.max(session.startTs, ts)));
  applyCursor(t, reframeDelayMs);
  if (t >= session.endTs && store().replay.playing) pauseReplay();
}

/** Relative seek in ms (keyboard; a backward step re-frames the chart once the keys go quiet). */
export function stepReplay(deltaMs: number): void {
  const c = store().replay.cursorTs;
  if (c != null) seekReplay(c + deltaMs, REFRAME_DEBOUNCE_MS);
}

/** Scrubber drag: pause while dragging, resume afterwards if it was playing. */
export function beginReplayScrub(): void {
  scrubbing = true;
  resumeAfterScrub = store().replay.playing;
  pauseReplay();
}
export function endReplayScrub(): void {
  scrubbing = false;
  if (needsReframe) scheduleReframe(0);
  // released at the end: stay there (showing the result) instead of restarting from the start
  const atEnd = !session || (store().replay.cursorTs ?? -Infinity) >= session.endTs;
  if (resumeAfterScrub && !atEnd) playReplay();
  resumeAfterScrub = false;
}

/** Replay the most recent finished window (the toolbar button). */
export function replayLatest(): void {
  const recent = store().recent;
  if (recent[0]) void startReplay(recent[0].id);
}

// ---- internals ----

function startTimer(): void {
  lastWall = wall();
  if (timer) return;
  timer = setInterval(step, STEP_MS);
}

function stopTimer(): void {
  if (timer) clearInterval(timer);
  timer = null;
}

function step(): void {
  const now = wall();
  const dt = now - lastWall;
  lastWall = now;
  const r = store().replay;
  if (!session || !r.active || !r.playing || r.loading) {
    stopTimer();
    return;
  }
  const next = Math.min(session.endTs, Math.round((r.cursorTs ?? session.startTs) + dt * r.speed));
  applyCursor(next);
  if (next >= session.endTs) pauseReplay();
}

/** Reveal the series up to `ts` and publish cursor + latest values in ONE store update. */
function applyCursor(ts: number, reframeDelayMs = 0): void {
  const s = session;
  if (!s) return;
  const kind = s.feed.seek(ts);
  const l = s.feed.latest();
  useTerminal.setState((st) => {
    const patch: Partial<TerminalState> = {
      replay: { ...st.replay, cursorTs: ts },
      lastTick: l.tick,
      lastOdds: l.odds,
      lastFair: l.fair,
    };
    if (kind === "rebuild") {
      patch.series = s.feed.out;
      patch.version = st.version + 1;
    } else if (kind === "append") patch.version = st.version + 1;
    return patch;
  });
  if (kind === "rebuild") {
    needsReframe = true;
    if (!scrubbing) scheduleReframe(reframeDelayMs);
  }
}

/**
 * Re-frame the chart after backward seeks. Removing data moves lightweight-charts' base index back without
 * compensating the right offset (it only compensates additions), so after a backward rebuild the view is
 * shifted left and playback would run off its right edge. Blank the series for one frame and reinstall them:
 * the chart treats that as a fresh load and frames the window again (previous window + this one). Debounced
 * for key repeats and deferred to the end of a scrub drag.
 */
function scheduleReframe(delayMs: number): void {
  if (reframeTimer) clearTimeout(reframeTimer);
  reframeTimer = setTimeout(() => {
    reframeTimer = null;
    void reframe();
  }, delayMs);
}

async function reframe(): Promise<void> {
  const s = session;
  if (!s || !needsReframe) return;
  needsReframe = false;
  reframing = true;
  try {
    useTerminal.setState((st) => ({ series: emptySeries(), version: st.version + 1 }));
    await nextFrame();
  } finally {
    reframing = false;
  }
  if (session !== s || !store().replay.active) return;
  const fresh = new ReplayFeed(s.feed.src);
  fresh.seek(s.feed.cursorTs);
  s.feed = fresh;
  useTerminal.setState((st) => ({ series: fresh.out, version: st.version + 1 }));
}

function cancelReframe(): void {
  if (reframeTimer) clearTimeout(reframeTimer);
  reframeTimer = null;
  needsReframe = false;
}

/**
 * While replaying, nothing else may own the chart's series or the current window. A socket reconnect during a
 * replay runs ws.loadHistory(), which installs live arrays: put the replay back (at the same cursor).
 */
function installGuard(): void {
  unguard?.();
  unguard = useTerminal.subscribe((st, prev) => {
    const s = session;
    if (!s || !st.replay.active || st.replay.loading || reframing) return;
    if (st.series !== prev.series && st.series !== s.feed.out) {
      const cursor = s.feed.cursorTs;
      const fresh = new ReplayFeed(s.feed.src);
      fresh.seek(cursor);
      s.feed = fresh;
      useTerminal.setState((x) => ({ series: fresh.out, version: x.version + 1 }));
    }
    if (st.current !== prev.current && st.current?.id !== s.windowId) useTerminal.setState({ current: s.data.window, next: null });
  });
}

function errorText(e: unknown): string {
  if (e && typeof e === "object" && "status" in e && (e as { status: number }).status === 404) return "Window not recorded";
  return e instanceof Error ? e.message : "Replay failed to load";
}

export type { ReplayState };
