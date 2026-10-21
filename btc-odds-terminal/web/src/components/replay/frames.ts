// Per-second replay frames (scrubber sparkline). OWNER: replay. buildFrames is the backend's own (shared/replay.ts).

export { buildFrames, FRAME_STALE_MS } from "@shared/replay";

/** Index of the frame at or before `ts` (frames ascending, 1s apart), or -1. */
export function frameIndexAt(frames: readonly { ts: number }[], ts: number): number {
  let lo = 0;
  let hi = frames.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (frames[mid].ts <= ts) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}
