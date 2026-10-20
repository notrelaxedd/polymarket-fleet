"use client";
import * as React from "react";
import { serverNow, useTerminal } from "./store";

/** Re-render on an interval and return the server-corrected now (ms). */
export function useServerNow(intervalMs = 1000): number {
  const [now, setNow] = React.useState(() => serverNow());
  React.useEffect(() => {
    setNow(serverNow());
    const id = setInterval(() => setNow(serverNow()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** ms remaining until `endTs`, refreshed every `intervalMs`, re-rendering only when the whole second changes. */
export function useCountdownMs(endTs: number | null | undefined, intervalMs = 250): number | null {
  const [ms, setMs] = React.useState<number | null>(null);
  React.useEffect(() => {
    if (endTs == null) {
      setMs(null);
      return;
    }
    const tick = () => {
      const left = Math.max(0, endTs - serverNow());
      setMs((prev) => (prev !== null && Math.ceil(prev / 1000) === Math.ceil(left / 1000) ? prev : left));
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [endTs, intervalMs]);
  return ms;
}

/** Latest odds-derived edge: prefers the fair snapshot's edge, else fair - upMid. */
export function useEdge(): number | null {
  return useTerminal((s) => {
    const f = s.lastFair;
    if (f?.edge != null) return f.edge;
    if (f && s.lastOdds?.upMid != null) return f.fair - s.lastOdds.upMid;
    return null;
  });
}
