// Live market state kept WHILE a replay owns the terminal store. OWNER: replay. Pure (no store, no DOM).
//
// During a replay the store's current / lastTick / lastOdds / lastFair / book follow the replay cursor and
// lib/ingest.ts drops live market messages. Anything that acts on the real market (position marks, Close /
// Edit bracket validation, new alerts) must read the live values instead: lib/replay.ts feeds a second
// subscription (mock stream / a second socket) through reduceLiveMarket() and exposes it as liveMarket() /
// useLiveMarket(). The muted live ticks are buffered here too so the tick history has no hole after exit.

import type { BookLevels, BtcTick, FairSnap, MarketWindow, OddsSnap, ServerMsg } from "@shared/protocol";

/** The live market fields (a structural subset of TerminalState, so selectOdds / selectFair accept it). */
export interface LiveMarket {
  current: MarketWindow | null;
  next: MarketWindow | null;
  lastTick: BtcTick | null;
  lastOdds: OddsSnap | null;
  lastFair: FairSnap | null;
  book: BookLevels | null;
}

export const EMPTY_LIVE_MARKET: LiveMarket = { current: null, next: null, lastTick: null, lastOdds: null, lastFair: null, book: null };

export function pickLiveMarket(s: LiveMarket): LiveMarket {
  return { current: s.current, next: s.next, lastTick: s.lastTick, lastOdds: s.lastOdds, lastFair: s.lastFair, book: s.book };
}

/** Apply one live ServerMsg (same rules as lib/ingest.ts for these fields). Returns `m` when nothing changed. */
export function reduceLiveMarket(m: LiveMarket, msg: ServerMsg): LiveMarket {
  switch (msg.type) {
    case "snapshot": {
      const s = msg.data;
      return { current: s.current, next: s.next, lastTick: s.lastTick, lastOdds: s.lastOdds, lastFair: s.lastFair, book: s.book ?? null };
    }
    case "btc":
      return { ...m, lastTick: msg.data };
    case "odds":
      return { ...m, lastOdds: msg.data };
    case "fair":
      return { ...m, lastFair: msg.data };
    case "book":
      return { ...m, book: msg.data };
    case "window": {
      const w = msg.data;
      const cur = m.current;
      let { current, next } = m;
      if (cur && cur.id === w.id) current = w;
      else if (w.status === "live" && (!cur || w.startTs > cur.startTs)) current = w;
      if (current?.id === w.id && next?.id === w.id) next = null;
      else if (next && next.id === w.id) next = w;
      else if (w.status === "upcoming" && w.startTs >= (current?.endTs ?? 0) && (!next || next.startTs < (current?.endTs ?? 0) || w.startTs < next.startTs)) next = w;
      return current === m.current && next === m.next ? m : { ...m, current, next };
    }
    case "rollover": {
      const { current, next } = msg.data;
      const book = m.book && m.book.windowId !== current?.id ? null : m.book;
      return { ...m, current, next, book };
    }
    default:
      return m;
  }
}

/**
 * Pre-replay ticks + the ticks buffered while muted, ascending, trimmed to `capMs` before the newest tick.
 * Buffered ticks win on overlap.
 */
export function mergeTicks(saved: readonly BtcTick[], buffered: readonly BtcTick[], capMs: number): BtcTick[] {
  const cut = buffered.length ? buffered[0].ts : Infinity;
  const out = saved.filter((t) => t.ts < cut);
  for (const t of buffered) out.push(t);
  if (!out.length) return out;
  const from = out[out.length - 1].ts - capMs;
  let i = 0;
  while (i < out.length && out[i].ts < from) i++;
  return i ? out.slice(i) : out;
}
