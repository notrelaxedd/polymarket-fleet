import type { Bar, FairSnap, MarketWindow, OddsSnap, ServerMsg } from "@shared/protocol";
import { notifyAlert } from "./notify";
import { useTerminal, type TerminalState } from "./store";

/**
 * Applies ServerMsgs to the store. Messages are queued and applied in ONE pass per animation frame:
 *  - series arrays (ticks/bars/odds/fair) are mutated in place (push / replace-last), `version` bumps once.
 *  - latest-value fields (lastTick, lastOdds, ...) are written with a single store update.
 *  - while a replay is active, live MARKET data (btc/odds/fair/window/rollover/book) is skipped (the replay owns
 *    series/current/lastOdds); `liveDirty` tells lib/replay.ts to resync on exit. Trading/alert msgs still apply.
 */

const REPLAY_MUTED = new Set<ServerMsg["type"]>(["btc", "odds", "fair", "window", "rollover", "book"]);
let liveDirty = false;

/** True when live market data was skipped during a replay (ws.resyncLive() needed); reading resets it. */
export function takeLiveDirty(): boolean {
  const d = liveDirty;
  liveDirty = false;
  return d;
}

/** bars/odds/fair kept in memory (the chart shows at most the 3h history plus live) */
export const SERIES_CAP_MS = 4 * 3600 * 1000;
/** raw ticks (up to 10/s, only drawn by the per-exchange overlay) are kept for a much shorter horizon */
export const TICK_CAP_MS = 30 * 60 * 1000;
const TRIM_SLACK = 2000; // only trim once we are this many elements over the cut
const RECENT_CAP = 50;

let queue: ServerMsg[] = [];
let rafId = 0;
let timerId: ReturnType<typeof setTimeout> | 0 = 0;

export function ingest(msg: ServerMsg): void {
  queue.push(msg);
  schedule();
}

function schedule() {
  if (rafId || timerId) return;
  if (typeof requestAnimationFrame === "function") {
    rafId = requestAnimationFrame(flush);
  }
  // rAF is paused in background tabs: make sure the queue still drains.
  timerId = setTimeout(flush, 500);
}

/** Replace the last element when `key` matches, else push. */
function upsertLast<T>(arr: T[], item: T, sameBucket: (last: T) => boolean) {
  const last = arr[arr.length - 1];
  if (last !== undefined && sameBucket(last)) arr[arr.length - 1] = item;
  else arr.push(item);
}

function trimFront<T>(arr: T[], ts: (x: T) => number, cutoff: number) {
  if (arr.length < TRIM_SLACK) return;
  if (ts(arr[0]) >= cutoff) return;
  let lo = 0;
  let hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ts(arr[mid]) < cutoff) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) arr.splice(0, lo);
}

export function flush(): void {
  if (rafId && typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId);
  if (timerId) clearTimeout(timerId);
  rafId = 0;
  timerId = 0;
  if (queue.length === 0) return;
  const msgs = queue;
  queue = [];

  const store = useTerminal;
  let patch: Partial<TerminalState> = {};
  let seriesDirty = false;
  const series = store.getState().series;

  const read = <K extends keyof TerminalState>(k: K): TerminalState[K] =>
    (k in patch ? (patch as TerminalState)[k] : store.getState()[k]) as TerminalState[K];

  const commit = () => {
    if (Object.keys(patch).length) store.setState(patch);
    patch = {};
  };

  const replaying = store.getState().replay.active;
  for (const m of msgs) {
    if (replaying && REPLAY_MUTED.has(m.type)) {
      liveDirty = true;
      continue;
    }
    switch (m.type) {
      case "snapshot": {
        commit();
        store.getState().applySnapshot(m.data);
        break;
      }
      case "btc": {
        const t = m.data;
        series.ticks.push(t);
        const bucket = Math.floor(t.ts / 1000) * 1000;
        const bars = series.bars;
        const last = bars[bars.length - 1];
        if (last && last.d === 1 && last.t === bucket) {
          // mutate the in-progress bar by replacing the element (stable array, new object)
          const nb: Bar = { t: last.t, d: 1, o: last.o, h: Math.max(last.h, t.price), l: Math.min(last.l, t.price), c: t.price };
          bars[bars.length - 1] = nb;
        } else if (!last || bucket > last.t) {
          const open = last ? last.c : t.price;
          bars.push({ t: bucket, d: 1, o: open, h: Math.max(open, t.price), l: Math.min(open, t.price), c: t.price });
        }
        patch.lastTick = t;
        seriesDirty = true;
        break;
      }
      case "odds": {
        const o: OddsSnap = m.data;
        const sec = Math.floor(o.ts / 1000);
        upsertLast(series.odds, o, (l) => Math.floor(l.ts / 1000) === sec);
        patch.lastOdds = o;
        seriesDirty = true;
        break;
      }
      case "fair": {
        const f: FairSnap = m.data;
        const sec = Math.floor(f.ts / 1000);
        upsertLast(series.fair, f, (l) => Math.floor(l.ts / 1000) === sec);
        patch.lastFair = f;
        seriesDirty = true;
        break;
      }
      case "window": {
        const w: MarketWindow = m.data;
        patch.windows = { ...read("windows"), [w.id]: w };
        const cur = read("current");
        const nxt = read("next");
        if (cur && cur.id === w.id) patch.current = w;
        else if (w.status === "live" && (!cur || w.startTs > cur.startTs)) patch.current = w;
        const curEnd = (patch.current ?? cur)?.endTs ?? 0;
        if (patch.current?.id === w.id && nxt?.id === w.id) patch.next = null; // next just went live
        else if (nxt && nxt.id === w.id) patch.next = w;
        else if (
          // a next window discovered after the last rollover only arrives as a `window` message
          w.status === "upcoming" &&
          w.startTs >= curEnd &&
          (!nxt || nxt.startTs < curEnd || w.startTs < nxt.startTs)
        ) {
          patch.next = w;
        }
        if (w.status === "closed" || w.status === "settled") {
          patch.recent = mergeRecent(read("recent"), w);
        }
        break;
      }
      case "rollover": {
        const { current, next } = m.data;
        const old = read("current");
        const windows = { ...read("windows") };
        if (current) windows[current.id] = current;
        if (next) windows[next.id] = next;
        let recent = read("recent");
        if (old && old.id !== current?.id) {
          const done = windows[old.id] ?? old;
          if (done.status === "live" || done.status === "upcoming") {
            recent = mergeRecent(recent, { ...done, status: "closed" });
          } else recent = mergeRecent(recent, done);
        }
        patch.current = current;
        patch.next = next;
        patch.windows = windows;
        patch.recent = recent;
        const book = read("book");
        if (book && book.windowId !== current?.id) patch.book = null;
        break;
      }
      case "feeds":
        patch.feeds = m.data;
        break;
      case "settings":
        patch.settings = m.data;
        break;
      // ---- steps 3-6 ----
      case "book":
        patch.book = m.data;
        break;
      case "orders":
        commit();
        store.getState().upsertOrders(m.data);
        break;
      case "fills":
        commit();
        store.getState().addFills(m.data);
        break;
      case "positions":
        commit();
        store.getState().upsertPositions(m.data);
        break;
      case "brackets":
        commit();
        store.getState().upsertBrackets(m.data);
        break;
      case "risk":
        patch.risk = m.data;
        break;
      case "live":
        patch.live = m.data;
        break;
      case "alerts":
        patch.alerts = m.data;
        break;
      case "alertFired":
        commit();
        store.getState().addAlertEvent(m.data);
        notifyAlert(m.data);
        break;
      case "drawings":
        commit();
        store.getState().setDrawings(m.data);
        break;
      case "notice":
        commit();
        store.getState().pushNotice(m.data);
        break;
    }
  }

  if (seriesDirty) {
    const now = patch.lastTick?.ts ?? Date.now();
    const cutoff = now - SERIES_CAP_MS;
    trimFront(series.ticks, (x) => x.ts, now - TICK_CAP_MS);
    trimFront(series.bars, (x) => x.t, cutoff);
    trimFront(series.odds, (x) => x.ts, cutoff);
    trimFront(series.fair, (x) => x.ts, cutoff);
    patch.version = store.getState().version + 1;
  }
  commit();
}

function mergeRecent(recent: MarketWindow[], w: MarketWindow): MarketWindow[] {
  const out = recent.filter((r) => r.id !== w.id);
  out.push(w);
  out.sort((a, b) => b.startTs - a.startTs);
  return out.slice(0, RECENT_CAP);
}
