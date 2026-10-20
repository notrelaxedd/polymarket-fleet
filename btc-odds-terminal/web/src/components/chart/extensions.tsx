"use client";

// Components rendered inside MainChart's ChartContext. Each one attaches its own primitive via
// `useChartPrimitive` and may render HTML overlays (absolutely positioned within the chart container).
//   <DrawingLayer/>   drawings + stickers        (chart/drawings/*, owner: drawings)
//   <PositionLines/>  entry / TP / SL lines      (chart/positions/*, owner: chart-positions)
// Position lines render after drawings so their drag handles win hit-testing.

import { DrawingLayer } from "./drawings/DrawingLayer";
import { PositionLines } from "./positions/PositionLines";

export function ChartExtensions() {
  return (
    <>
      <DrawingLayer />
      <PositionLines />
    </>
  );
}
