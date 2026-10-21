// Progressive reveal of a recorded window for replay. OWNER: replay. Pure (no store, no DOM) so it is unit-testable.
//
// The chart reacts to the store's `series` exactly like it does to live data:
//   - a NEW array identity  -> full rebuild (history load)            -> seek() returns "rebuild"
//   - in-place push / replace-last + a `version` bump -> tail update  -> seek() returns "append"
// So moving the cursor forward mutates `out`'s arrays in place, and moving it backward builds fresh arrays.
//
// What is visible at cursor c:
//   - bars whose second (or seeded minute) has closed: t + d·1s <= c;
//   - plus, when raw ticks are available, the in-progress 1s bar built from the ticks seen so far (so candles
//     form tick by tick like live); without ticks the chart moves one closed second at a time;
//   - ticks / odds / fair with ts <= c.

import type { Bar, BtcTick, FairSnap, OddsSnap } from "@shared/protocol";

export interface ReplaySeries {
  bars: Bar[];
  ticks: BtcTick[];
  odds: OddsSnap[];
  fair: FairSnap[];
}

export type SeekResult = "none" | "append" | "rebuild";

/** lastTick older than this falls back to the latest visible bar's close */
export const TICK_FRESH_MS = 5_000;

export const emptySeries = (): ReplaySeries => ({ bars: [], ticks: [], odds: [], fair: [] });

export class ReplayFeed {
  /** the arrays installed in the store (mutated in place on forward seeks, replaced on backward seeks) */
  out: ReplaySeries = emptySeries();
  private cursor = -Infinity;
  private ib = 0;
  private it = 0;
  private io = 0;
  private iF = 0;
  /** out.bars' last element is an in-progress bar built from ticks */
  private partial = false;

  constructor(readonly src: Readonly<ReplaySeries>) {}

  get cursorTs(): number {
    return this.cursor;
  }

  /** Reveal everything up to `ts`. "rebuild" = `out` holds new arrays; "append" = `out` changed in place. */
  seek(ts: number): SeekResult {
    // the first seek fills fresh arrays the caller has not installed yet
    let res: SeekResult = this.cursor === -Infinity ? "rebuild" : "none";
    if (ts < this.cursor) {
      this.out = emptySeries();
      this.ib = this.it = this.io = this.iF = 0;
      this.partial = false;
      res = "rebuild";
    }
    this.cursor = ts;
    const { src, out } = this;
    let changed = false;

    if (this.partial) {
      out.bars.pop();
      this.partial = false;
      changed = true;
    }
    while (this.ib < src.bars.length && src.bars[this.ib].t + src.bars[this.ib].d * 1000 <= ts) {
      out.bars.push(src.bars[this.ib++]);
      changed = true;
    }
    while (this.it < src.ticks.length && src.ticks[this.it].ts <= ts) {
      out.ticks.push(src.ticks[this.it++]);
      changed = true;
    }
    const pb = this.partialBar(ts);
    if (pb) {
      out.bars.push(pb);
      this.partial = true;
      changed = true;
    }
    while (this.io < src.odds.length && src.odds[this.io].ts <= ts) {
      out.odds.push(src.odds[this.io++]);
      changed = true;
    }
    while (this.iF < src.fair.length && src.fair[this.iF].ts <= ts) {
      out.fair.push(src.fair[this.iF++]);
      changed = true;
    }
    return res === "rebuild" ? res : changed ? "append" : "none";
  }

  /** Values the header / legend show at the cursor. */
  latest(): { tick: BtcTick | null; odds: OddsSnap | null; fair: FairSnap | null } {
    const { out } = this;
    const ts = this.cursor;
    let tick: BtcTick | null = out.ticks[out.ticks.length - 1] ?? null;
    if (!tick || ts - tick.ts > TICK_FRESH_MS) {
      const b = out.bars[out.bars.length - 1];
      tick = b ? { ts: Math.min(ts, b.t + b.d * 1000), price: b.c, sources: {} } : null;
    }
    return { tick, odds: out.odds[out.odds.length - 1] ?? null, fair: out.fair[out.fair.length - 1] ?? null };
  }

  /** The 1s bar containing `ts`, from the ticks seen so far (null without ticks for it). */
  private partialBar(ts: number): Bar | null {
    const b = this.src.bars[this.ib];
    if (!b || b.d !== 1 || b.t > ts) return null;
    const ticks = this.src.ticks;
    let lo = 0;
    let hi = this.it;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ticks[mid].ts < b.t) lo = mid + 1;
      else hi = mid;
    }
    if (lo >= this.it) return null;
    let h = b.o;
    let l = b.o;
    let c = b.o;
    for (let k = lo; k < this.it; k++) {
      const p = ticks[k].price;
      if (p > h) h = p;
      if (p < l) l = p;
      c = p;
    }
    return { t: b.t, d: 1, o: b.o, h, l, c };
  }
}
