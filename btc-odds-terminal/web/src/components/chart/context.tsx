"use client";

// Extension point for later build steps (position lines with draggable TP/SL, drawings, stickers…).
//
// MainChart provides a ChartHandle through React context to every component listed in
// `extensions.tsx`. An extension typically writes one primitive class under `primitives/` and attaches it:
//
//   export function PositionLines() {
//     useChartPrimitive((h) => new PositionLinesPrimitive(h), "odds");
//     return null;
//   }
//
// "btc" primitives live on the right price scale (BTC USD), "odds" primitives on the left (0–100¢).

import { createContext, useContext, useEffect, useRef, type MutableRefObject } from "react";
import type { IChartApi, ISeriesApi, ISeriesPrimitive, Time } from "lightweight-charts";
import type { CandleAgg, FairAgg, OddsAgg } from "./aggregate";

export type ScaleKey = "btc" | "odds";

export interface ChartHandle {
  readonly chart: IChartApi;
  /** container element (position: relative) — HTML overlays can be appended here */
  readonly container: HTMLElement;
  /** Right-scale anchor series (BTC close line, always present; hidden in candle mode). */
  readonly btcSeries: ISeriesApi<"Line">;
  /** Left-scale anchor series (Up mid line, always present). */
  readonly oddsSeries: ISeriesApi<"Line">;
  /** Current timeframe in seconds. */
  timeframe(): number;
  /** Epoch ms -> pane x (CSS px). Works for any time, including between/after data points. */
  timeToX(ms: number): number | null;
  /** Pane x -> epoch ms (bucket-interpolated). */
  xToTime(x: number): number | null;
  /** BTC USD <-> pane y, cents <-> pane y (main pane). */
  btcToY(price: number): number | null;
  yToBtc(y: number): number | null;
  centsToY(c: number): number | null;
  yToCents(y: number): number | null;
  /** Attach a series primitive to the BTC (right) or odds (left) scale. Returns a detach function. */
  attach(primitive: ISeriesPrimitive<Time>, scale: ScaleKey): () => void;
  /** Aggregated (timeframe-bucketed) data, ascending by time (seconds). Read-only. */
  data(): { candles: readonly CandleAgg[]; odds: readonly OddsAgg[]; fair: readonly FairAgg[] };
  /** Ask every primitive to recompute + repaint. */
  requestRedraw(): void;
}

export const ChartContext = createContext<ChartHandle | null>(null);

export function useChart(): ChartHandle | null {
  return useContext(ChartContext);
}

/**
 * Attach a primitive for the lifetime of the calling component (re-created when deps change).
 * `factory` receives the chart handle so the primitive can convert coordinates.
 */
export function useChartPrimitive<P extends ISeriesPrimitive<Time>>(
  factory: (h: ChartHandle) => P,
  scale: ScaleKey,
  deps: readonly unknown[] = [],
): MutableRefObject<P | null> {
  const h = useChart();
  const ref = useRef<P | null>(null);
  useEffect(() => {
    if (!h) return;
    const p = factory(h);
    ref.current = p;
    const detach = h.attach(p, scale);
    return () => {
      detach();
      ref.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [h, scale, ...deps]);
  return ref;
}
