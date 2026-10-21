"use client";
// Replay scrubber: the window's Up mid (blue) and fair value (purple, dashed) as a sparkline, my fills as ticks,
// the played part tinted, a draggable cursor and a read-out (mouse: at the pointer; keyboard focus and touch
// drags: at the cursor). Keyboard: ←/→ 5s (Shift 30s), PgUp/PgDn 60s, Home/End, Space/Enter play/pause.
// The visual track is 28px; on coarse pointers the hit area is 44px tall. OWNER: replay.
import { useCallback, useId, useMemo, useRef, useState } from "react";
import type { Fill, ReplayFrame } from "@shared/protocol";
import { etTimeSec, mmss, quoteCents } from "@/lib/format";
import { beginReplayScrub, endReplayScrub, seekReplay, stepReplay, toggleReplayPlaying } from "@/lib/replay";
import { cn } from "@/lib/utils";
import { frameIndexAt } from "./frames";

const VB_W = 1000;
const VB_H = 100;
/** sparkline vertical padding inside the track (viewBox units) */
const PAD_Y = 14;

interface Props {
  startTs: number;
  endTs: number;
  cursorTs: number;
  /** end of the window itself (> endTs while a live window is only partly recorded) */
  windowEndTs: number;
  frames: readonly ReplayFrame[];
  fills: readonly Fill[];
  disabled?: boolean;
  className?: string;
}

export function Scrubber({ startTs, endTs, cursorTs, windowEndTs, frames, fills, disabled, className }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [hoverTs, setHoverTs] = useState<number | null>(null);
  /** show the read-out at the cursor (keyboard focus, touch / pen drag) */
  const [cursorReadout, setCursorReadout] = useState(false);
  const clipId = `replay-played-${useId().replace(/:/g, "")}`;
  // the track spans the whole window; a partly recorded (live) window shows its unrecorded rest hatched
  const span = Math.max(1, windowEndTs - startTs);
  const frac = (ts: number) => Math.min(1, Math.max(0, (ts - startTs) / span));

  const paths = useMemo(() => sparkPaths(frames, startTs, span), [frames, startTs, span]);

  const tsAt = useCallback(
    (clientX: number): number | null => {
      const el = trackRef.current;
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (r.width <= 0) return null;
      const f = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
      return startTs + f * span;
    },
    [startTs, span],
  );

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || e.button !== 0) return;
    e.preventDefault();
    ref.current?.focus();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragging.current = true;
    if (e.pointerType !== "mouse") setCursorReadout(true);
    beginReplayScrub();
    const ts = tsAt(e.clientX);
    if (ts != null) seekReplay(ts);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const ts = tsAt(e.clientX);
    if (e.pointerType === "mouse") setHoverTs(ts);
    if (dragging.current && ts != null) seekReplay(ts);
  };
  const finish = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    dragging.current = false;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    if (e.pointerType !== "mouse") setCursorReadout(false);
    endReplayScrub();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (!e.repeat) toggleReplayPlaying();
      return;
    }
    const big = e.shiftKey ? 30_000 : 5_000;
    const map: Record<string, () => void> = {
      ArrowRight: () => stepReplay(big),
      ArrowUp: () => stepReplay(big),
      ArrowLeft: () => stepReplay(-big),
      ArrowDown: () => stepReplay(-big),
      PageUp: () => stepReplay(60_000),
      PageDown: () => stepReplay(-60_000),
      Home: () => seekReplay(startTs),
      End: () => seekReplay(endTs),
    };
    const fn = map[e.key];
    if (!fn) return;
    e.preventDefault();
    setCursorReadout(true);
    fn();
  };
  const onFocus = (e: React.FocusEvent<HTMLDivElement>) => {
    let kbd = false;
    try {
      kbd = e.currentTarget.matches(":focus-visible");
    } catch {}
    if (kbd) setCursorReadout(true);
  };

  const cursorFrame = frames.length ? frames[frameIndexAt(frames, Math.min(cursorTs, endTs))] : undefined;
  const readoutTs = hoverTs != null && !dragging.current ? hoverTs : cursorReadout ? cursorTs : null;
  const readoutFrame = readoutTs != null ? frames[frameIndexAt(frames, Math.min(readoutTs, endTs))] : undefined;
  const xCursor = frac(cursorTs) * 100;
  const xRecorded = frac(endTs) * 100;

  return (
    <div
      ref={ref}
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label="Replay position"
      aria-valuemin={startTs}
      aria-valuemax={endTs}
      aria-valuenow={Math.round(cursorTs)}
      aria-valuetext={
        `${etTimeSec(cursorTs)} ET, ${mmss(cursorTs - startTs)} of ${mmss(windowEndTs - startTs)}` +
        (cursorFrame?.upMid != null ? `, Up ${quoteCents(cursorFrame.upMid)}` : "") +
        (cursorFrame?.fair != null ? `, Fair ${quoteCents(cursorFrame.fair)}` : "")
      }
      aria-disabled={disabled || undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finish}
      onPointerCancel={finish}
      onPointerLeave={() => setHoverTs(null)}
      onKeyDown={onKeyDown}
      onFocus={onFocus}
      onBlur={() => setCursorReadout(false)}
      className={cn(
        "group flex h-7 min-w-[120px] cursor-pointer touch-none select-none items-center outline-none [@media(pointer:coarse)]:h-[44px]",
        disabled && "cursor-default opacity-60",
        className,
      )}
    >
      <div
        ref={trackRef}
        className={cn(
          "relative h-7 w-full rounded-ctl border border-border-strong bg-inset",
          "group-focus-visible:border-odds group-focus-visible:ring-1 group-focus-visible:ring-odds/60",
        )}
      >
        <div className="absolute inset-0 overflow-hidden rounded-[7px]">
          {/* played part */}
          <div className="absolute inset-y-0 left-0 bg-odds-tint" style={{ width: `${xCursor}%` }} />
          {/* not recorded yet (live window) */}
          {xRecorded < 100 && (
            <div
              className="absolute inset-y-0 right-0 bg-[repeating-linear-gradient(135deg,#1E2530_0_2px,transparent_2px_6px)]"
              style={{ left: `${xRecorded}%` }}
            />
          )}
          <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${VB_W} ${VB_H}`} preserveAspectRatio="none" aria-hidden>
            <line x1={0} x2={VB_W} y1={VB_H / 2} y2={VB_H / 2} stroke="#2A3442" strokeDasharray="4 6" vectorEffect="non-scaling-stroke" />
            <defs>
              <clipPath id={clipId}>
                <rect x={0} y={0} width={(xCursor / 100) * VB_W} height={VB_H} />
              </clipPath>
            </defs>
            {/* the whole window dimmed (navigation), the played part at full strength */}
            <g opacity={0.35}>
              <Spark paths={paths} />
            </g>
            <g clipPath={`url(#${clipId})`}>
              <Spark paths={paths} />
            </g>
          </svg>
          {fills.map((f) => (
            <span
              key={f.id}
              className={cn("absolute bottom-0 h-2 w-[2px] -translate-x-1/2 rounded-t-sm", f.side === "up" ? "bg-up" : "bg-down")}
              style={{ left: `${frac(f.ts) * 100}%` }}
              title={`${f.action === "buy" ? "Buy" : "Sell"} ${f.side === "up" ? "Up" : "Down"} ${f.qty} @ ${quoteCents(f.priceCents)} · ${etTimeSec(f.ts)}`}
            />
          ))}
        </div>
        {/* cursor */}
        <div className="pointer-events-none absolute inset-y-[-3px] w-0" style={{ left: `${xCursor}%` }}>
          <div className="absolute inset-y-0 left-[-1px] w-[2px] rounded-full bg-text" />
          <div className="absolute left-[-5px] top-[-2px] h-[10px] w-[10px] rounded-full border-2 border-bg bg-text" />
        </div>
        {/* read-out (decorative: the slider's aria-valuetext carries the position) */}
        {readoutTs != null && (
          <div
            aria-hidden
            className="pointer-events-none absolute bottom-full z-20 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-ctl border border-border-strong bg-surface-2 px-1.5 py-0.5 text-[11px] shadow-lg"
            style={{ left: `${Math.min(92, Math.max(8, frac(readoutTs) * 100))}%` }}
          >
            <span className="num text-text-2">{etTimeSec(readoutTs)}</span>
            {readoutFrame?.upMid != null && <span className="num ml-1.5 text-odds-text">Up {quoteCents(readoutFrame.upMid)}</span>}
            {readoutFrame?.fair != null && <span className="num ml-1.5 text-fair">Fair {quoteCents(readoutFrame.fair)}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function Spark({ paths }: { paths: { mid: string; fair: string } }) {
  return (
    <>
      {paths.fair && (
        <path d={paths.fair} fill="none" stroke="#C29BFF" strokeOpacity={0.8} strokeWidth={1} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      )}
      {paths.mid && <path d={paths.mid} fill="none" stroke="#5B9CFF" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />}
    </>
  );
}

/** SVG paths (viewBox 1000×100) for Up mid and fair over the window; gaps (null) break the line. */
function sparkPaths(frames: readonly ReplayFrame[], startTs: number, span: number): { mid: string; fair: string } {
  const x = (ts: number) => (((ts - startTs) / span) * VB_W).toFixed(1);
  const y = (c: number) => (PAD_Y + (1 - Math.min(100, Math.max(0, c)) / 100) * (VB_H - 2 * PAD_Y)).toFixed(1);
  // ≤ ~1 point per viewBox unit is plenty
  const stride = Math.max(1, Math.floor(frames.length / VB_W));
  const build = (pick: (f: ReplayFrame) => number | null) => {
    let d = "";
    let pen = false;
    for (let i = 0; i < frames.length; i += stride) {
      const v = pick(frames[i]);
      if (v == null) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(frames[i].ts)} ${y(v)}`;
      pen = true;
    }
    return d;
  };
  return { mid: build((f) => f.upMid), fair: build((f) => f.fair) };
}
