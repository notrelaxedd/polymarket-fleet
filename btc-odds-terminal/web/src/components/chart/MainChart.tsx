"use client";

// Main chart: BTC (right scale) + current market's Up odds (left scale, 0–100¢) on one time axis, with
// window bands, price to beat, now line, crosshair tooltip and the edge pane. Fills its parent.
// All drawing logic lives in ChartEngine + primitives/; this component only owns the lifecycle and
// exposes the chart to extensions (see context.tsx / extensions.tsx).

import { useEffect, useRef, useState } from "react";
import { ChartContext, type ChartHandle } from "./context";
import { ChartEngine } from "./engine";
import { ChartExtensions } from "./extensions";
import { C } from "./theme";

export default function MainChart() {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<HTMLDivElement>(null);
  const [handle, setHandle] = useState<ChartHandle | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    const el = chartRef.current;
    if (!host || !el) return;
    const engine = new ChartEngine(host, el);
    setHandle(engine);
    return () => {
      setHandle(null);
      engine.destroy();
    };
  }, []);

  return (
    <div
      ref={hostRef}
      role="region"
      aria-label="BTC price and Up odds chart"
      style={{ position: "relative", width: "100%", height: "100%", minHeight: 0, minWidth: 0, overflow: "hidden", background: C.bg }}
    >
      <div ref={chartRef} style={{ position: "absolute", inset: 0 }} />
      <ChartContext.Provider value={handle}>
        <ChartExtensions />
      </ChartContext.Provider>
    </div>
  );
}
