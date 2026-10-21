// Crosshair -> store hover dedupe. The crosshair callback re-fires on every data update while the cursor
// rests on the chart, so the hovered (possibly in-progress) candle is compared by VALUE, not by time:
// the legend follows a live candle, and a timeframe/history swap that keeps the same bucket time updates too.

import type { HoverInfo } from "@/lib/store";

const KEYS: readonly (keyof HoverInfo)[] = [
  "time",
  "o",
  "h",
  "l",
  "c",
  "upMid",
  "upBid",
  "upAsk",
  "fair",
  "edge",
  "ptb",
  "tLeftSec",
];

export function sameHover(a: HoverInfo | null, b: HoverInfo | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  for (const k of KEYS) {
    // whole seconds are enough for the countdown; avoids a store write per frame for sub-second drift
    if (k === "tLeftSec") {
      const x = a[k];
      const y = b[k];
      if ((x == null ? null : Math.floor(x)) !== (y == null ? null : Math.floor(y))) return false;
    } else if (a[k] !== b[k]) return false;
  }
  return true;
}
