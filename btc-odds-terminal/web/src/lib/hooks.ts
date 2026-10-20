"use client";
import * as React from "react";
import { selectFair, selectOdds, serverNow, useTerminal } from "./store";

/** Current window's top of book (null until a quote for this window arrives). */
export const useOdds = () => useTerminal(selectOdds);

/** Current window's fair value (null until one for this window arrives). */
export const useFair = () => useTerminal(selectFair);

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

/** Current window's edge: prefers the fair snapshot's edge, else fair - upMid (both of this window). */
export function useEdge(): number | null {
  return useTerminal((s) => {
    const f = selectFair(s);
    if (f?.edge != null) return f.edge;
    const upMid = selectOdds(s)?.upMid;
    if (f && upMid != null) return f.fair - upMid;
    return null;
  });
}
