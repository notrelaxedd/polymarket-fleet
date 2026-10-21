// Shaded Up bid/ask band (16% of the odds blue) under the Up mid line. Attached to the left-scale
// (odds) anchor series, so prices are cents. The band breaks at window changes and missing quotes.

import { lowerBoundTime } from "../aggregate";
import { C } from "../theme";
import { BasePrimitive, FnPaneView, FnRenderer, type MediaScope } from "./base";

export class OddsBandPrimitive extends BasePrimitive {
  /** flat [x, yAsk, yBid, x, yAsk, yBid, ...] per contiguous segment */
  private segments: number[][] = [];
  private readonly views = [new FnPaneView(new FnRenderer((s) => this.draw(s)), "bottom")];

  paneViews() {
    return this.views;
  }

  updateAllViews() {
    this.segments = [];
    const chart = this.chart;
    const series = this.series;
    if (!this.enabled || !chart || !series) return;
    const ts = chart.timeScale();
    const vr = ts.getVisibleRange();
    if (!vr) return;
    const odds = this.h.data().odds;
    if (!odds.length) return;
    const from = Math.max(0, lowerBoundTime(odds, vr.from as number) - 1);
    const to = Math.min(odds.length, lowerBoundTime(odds, vr.to as number) + 2);
    let seg: number[] = [];
    let win = "";
    const flush = () => {
      if (seg.length >= 6) this.segments.push(seg);
      seg = [];
    };
    for (let i = from; i < to; i++) {
      const o = odds[i];
      if (o.windowId !== win) {
        flush();
        win = o.windowId;
      }
      if (o.upBid == null || o.upAsk == null) {
        flush();
        continue;
      }
      const x = ts.timeToCoordinate(o.time as never);
      const ya = series.priceToCoordinate(o.upAsk);
      const yb = series.priceToCoordinate(o.upBid);
      if (x == null || ya == null || yb == null) {
        flush();
        continue;
      }
      seg.push(x, ya, yb);
    }
    flush();
  }

  private draw({ ctx }: MediaScope) {
    if (!this.segments.length) return;
    ctx.fillStyle = C.oddsBand;
    for (const s of this.segments) {
      ctx.beginPath();
      ctx.moveTo(s[0], s[1]);
      for (let i = 3; i < s.length; i += 3) ctx.lineTo(s[i], s[i + 1]);
      for (let i = s.length - 3; i >= 0; i -= 3) ctx.lineTo(s[i], s[i + 2]);
      ctx.closePath();
      ctx.fill();
    }
  }
}
