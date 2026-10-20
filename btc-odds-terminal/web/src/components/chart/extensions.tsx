"use client";

// Components rendered inside MainChart's ChartContext. Later build steps add their chart extensions
// here (e.g. <PositionLines/>, <Drawings/>, <Stickers/>); each one attaches its own primitive via
// `useChartPrimitive` and may render HTML overlays (absolutely positioned within the chart container).

export function ChartExtensions() {
  return null;
}
