// Time/price <-> pane-pixel conversion for drawings. Drawings are anchored to (epoch ms, price) so they
// survive timeframe changes; this wraps the ChartHandle conversions and extends xToTime past both ends of
// the loaded data (the time scale is uniform there: one bucket of `timeframe` seconds per bar).

import type { Logical } from "lightweight-charts";
import type { DrawingPoint, DrawingScale } from "@shared/protocol";
import type { ChartHandle } from "../context";
import type { View, XY } from "./geometry";
import { makePoint } from "./model";

export class Coords {
  constructor(private readonly h: ChartHandle) {}

  /** main pane size in CSS px */
  view(): View {
    const width = this.h.chart.timeScale().width();
    const pane = this.h.chart.panes()[0];
    return { width, height: pane ? pane.getHeight() : 0 };
  }

  /** pane origin inside the chart element (the left price scale sits before it) */
  paneLeft(): number {
    try {
      return this.h.chart.priceScale("left").width();
    } catch {
      return 0;
    }
  }

  priceToY(price: number, scale: DrawingScale): number | null {
    return scale === "odds" ? this.h.centsToY(price) : this.h.btcToY(price);
  }

  yToPrice(y: number, scale: DrawingScale): number | null {
    return scale === "odds" ? this.h.yToCents(y) : this.h.yToBtc(y);
  }

  timeToX(ms: number): number | null {
    return this.h.timeToX(ms);
  }

  private barSpacing(): number {
    const ts = this.h.chart.timeScale();
    const a = ts.logicalToCoordinate(0 as Logical);
    const b = ts.logicalToCoordinate(1 as Logical);
    return a != null && b != null && b - a > 0 ? b - a : 6;
  }

  /** pane x -> epoch ms, extrapolated linearly beyond the loaded buckets */
  xToTime(x: number): number | null {
    const t = this.h.xToTime(x);
    if (t != null && Number.isFinite(t)) return t;
    const candles = this.h.data().candles;
    if (!candles.length) return null;
    const tfMs = (this.h.timeframe() || 1) * 1000;
    const first = candles[0].time * 1000;
    const last = candles[candles.length - 1].time * 1000;
    const xf = this.h.timeToX(first);
    const xl = this.h.timeToX(last);
    const ref = xl != null && (xf == null || x >= xl) ? { ms: last, x: xl } : xf != null ? { ms: first, x: xf } : null;
    if (!ref) return null;
    return ref.ms + ((x - ref.x) / this.barSpacing()) * tfMs;
  }

  toXY(p: DrawingPoint): XY | null {
    const x = this.timeToX(p.time);
    const y = this.priceToY(p.price, p.scale);
    return x == null || y == null ? null : { x, y };
  }

  toPoint(x: number, y: number, scale: DrawingScale): DrawingPoint | null {
    const time = this.xToTime(x);
    const price = this.yToPrice(y, scale);
    return time == null || price == null ? null : makePoint(time, price, scale);
  }
}
