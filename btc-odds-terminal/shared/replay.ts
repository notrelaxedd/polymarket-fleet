// Pure replay helpers: per-second replay frames and tick thinning. OWNER: replay.
// Shared by GET /api/replay/:windowId (backend/src/replay), the web mock (lib/mock/replay.ts) and the web client
// (payloads without frames). Extensionless imports (tsx + Next.js).

import type { Bar, BtcTick, FairSnap, OddsSnap, ReplayFrame } from "./protocol";

/** A frame shows a quote / fair value only while it is at most this old (older = feed gap -> null). */
export const FRAME_STALE_MS = 5_000;
/** Raw ticks are thinned to the last tick of each slot of this size (live streams up to ~10/s). */
export const TICK_SLOT_MS = 250;

/**
 * One frame per whole second in [fromTs, toTs] (both rounded to the second): the BTC close of the latest bar
 * that has closed by then, and the latest Up quote / fair value at or before it (null once older than
 * FRAME_STALE_MS). `edge` = fair - Up mid from the frame's own values, so it always matches what it shows.
 * Inputs must be ascending (as the DB returns them). O(frames + inputs).
 */
export function buildFrames(bars: readonly Bar[], odds: readonly OddsSnap[], fair: readonly FairSnap[], fromTs: number, toTs: number): ReplayFrame[] {
  const out: ReplayFrame[] = [];
  const start = Math.ceil(fromTs / 1000) * 1000;
  const end = Math.floor(toTs / 1000) * 1000;
  let ib = 0;
  let io = 0;
  let iF = 0;
  let btc: number | null = null;
  let o: OddsSnap | null = null;
  let f: FairSnap | null = null;
  for (let ts = start; ts <= end; ts += 1000) {
    while (ib < bars.length && bars[ib].t + bars[ib].d * 1000 <= ts) btc = bars[ib++].c;
    while (io < odds.length && odds[io].ts <= ts) o = odds[io++];
    while (iF < fair.length && fair[iF].ts <= ts) f = fair[iF++];
    const q = o && ts - o.ts <= FRAME_STALE_MS ? o : null;
    const fv = f && ts - f.ts <= FRAME_STALE_MS ? f.fair : null;
    const upMid = q?.upMid ?? null;
    out.push({
      ts,
      btc,
      upBid: q?.upBid ?? null,
      upAsk: q?.upAsk ?? null,
      upMid,
      fair: fv,
      edge: fv != null && upMid != null ? round1(fv - upMid) : null,
    });
  }
  return out;
}

/** Keep the last tick of every TICK_SLOT_MS slot (ascending input -> ascending output). */
export function thinTicks(ticks: readonly BtcTick[], slotMs = TICK_SLOT_MS): BtcTick[] {
  const out: BtcTick[] = [];
  let slot = -Infinity;
  for (const t of ticks) {
    const s = Math.floor(t.ts / slotMs);
    if (s === slot) out[out.length - 1] = t;
    else {
      out.push(t);
      slot = s;
    }
  }
  return out;
}

const round1 = (x: number) => Math.round(x * 10) / 10;
