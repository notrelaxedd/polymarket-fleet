// Window lookups shared by primitives and the tooltip.
import type { MarketWindow } from "@shared/protocol";
import type { TerminalState } from "@/lib/store";

type WinState = Pick<TerminalState, "windows" | "current" | "next" | "recent">;

let memoKey: unknown[] = [];
let memo: MarketWindow[] = [];

/** Every known window, ascending by start, de-duplicated (current/next/recent override `windows`). */
export function windowList(st: WinState): MarketWindow[] {
  const key = [st.windows, st.current, st.next, st.recent];
  if (key.every((k, i) => k === memoKey[i])) return memo;
  const byId = new Map<string, MarketWindow>();
  for (const w of Object.values(st.windows)) byId.set(w.id, w);
  for (const w of st.recent) byId.set(w.id, w);
  if (st.next) byId.set(st.next.id, st.next);
  if (st.current) byId.set(st.current.id, st.current);
  memo = [...byId.values()].sort((a, b) => a.startTs - b.startTs);
  memoKey = key;
  return memo;
}

/** Window with startTs <= ms < endTs (latest start wins), or null. */
export function windowAt(list: readonly MarketWindow[], ms: number): MarketWindow | null {
  let lo = 0;
  let hi = list.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].startTs <= ms) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  for (let i = ans; i >= 0 && i >= ans - 2; i--) {
    const w = list[i];
    if (ms >= w.startTs && ms < w.endTs) return w;
  }
  return null;
}

/** The window that ended where `current` started (falls back to the newest finished one). */
export function previousWindow(list: readonly MarketWindow[], current: MarketWindow | null): MarketWindow | null {
  if (!current) return null;
  let best: MarketWindow | null = null;
  for (const w of list) {
    if (w.id === current.id) continue;
    if (w.endTs <= current.startTs && (!best || w.endTs > best.endTs)) best = w;
  }
  return best;
}
