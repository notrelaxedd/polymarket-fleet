import type { Bar } from "../../../shared/protocol.ts";

/** Aggregates (ts, price) points into fixed-duration OHLC bars. */
export class BarAggregator {
  private cur: Bar | null = null;
  private readonly durMs: number;

  constructor(private readonly durSec = 1) {
    this.durMs = durSec * 1000;
  }

  /** Returns the bar that just CLOSED when ts crosses into a newer bucket, else null. Out-of-order points fold into the current bar. */
  push(ts: number, price: number): Bar | null {
    const t = Math.floor(ts / this.durMs) * this.durMs;
    const cur = this.cur;
    if (!cur) {
      this.cur = { t, d: this.durSec, o: price, h: price, l: price, c: price };
      return null;
    }
    if (t <= cur.t) {
      if (price > cur.h) cur.h = price;
      if (price < cur.l) cur.l = price;
      cur.c = price;
      return null;
    }
    const closed = cur;
    this.cur = { t, d: this.durSec, o: price, h: price, l: price, c: price };
    return { ...closed };
  }

  /** The in-progress bar (a copy), or null before the first push. */
  current(): Bar | null {
    return this.cur ? { ...this.cur } : null;
  }
}
