"use client";
// Replay transport bar, shown above the legend while store.replay.active:
//   REPLAY badge · window (ET) · play/pause · 1×/4×/16× · scrubber · elapsed / length · result at the end · Exit.
// Phones: badge, window and Exit on the first row, the transport on a full-width second row (play, speed,
// elapsed and the result chip; the scrubber gets its own full-width row below, and the rows wrap rather than
// scroll). Keyboard: Esc anywhere in the bar exits the replay (focus then moves to the toolbar's Replay window
// button); Space/Enter on the scrubber play / pause. OWNER: replay.
import { useRef } from "react";
import { Loader2, Pause, Play, RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { Tip } from "@/components/ui/tooltip";
import { etRange, etTimeSec, mmss } from "@/lib/format";
import {
  REPLAY_SPEEDS,
  setReplaySpeed,
  startReplay,
  stopReplay,
  toggleReplayPlaying,
  useReplayInfo,
} from "@/lib/replay";
import { useTerminal, type ReplaySpeed } from "@/lib/store";
import { cn } from "@/lib/utils";
import { Scrubber } from "./Scrubber";

const SPEED_OPTIONS = REPLAY_SPEEDS.map((v) => ({ value: v, label: `${v}×`, ariaLabel: `${v}x speed` }));

/** Amber "REPLAY" pill (also meant for the header next to PAPER / LIVE while a replay runs). */
export function ReplayBadge({ className }: { className?: string }) {
  const active = useTerminal((s) => s.replay.active);
  if (!active) return null;
  return (
    <span
      className={cn(
        "inline-flex h-[22px] shrink-0 items-center gap-1.5 rounded-[6px] border border-ptb/40 bg-ptb-tint px-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ptb",
        className,
      )}
    >
      <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-ptb" />
      Replay
    </span>
  );
}

/** The toolbar's "Replay window" button (ChartToolbar), where focus goes after leaving a replay. */
const REPLAY_BUTTON_SELECTOR = 'button[aria-label="Replay window"]';

export function ReplayBar() {
  const r = useTerminal((s) => s.replay);
  const info = useReplayInfo();
  const barRef = useRef<HTMLDivElement>(null);
  if (!r.active) return null;

  const exit = async () => {
    const el = barRef.current;
    const hadFocus = !!el && typeof document !== "undefined" && el.contains(document.activeElement);
    await stopReplay();
    if (!hadFocus) return;
    // the button re-enables once the replay is gone (next render)
    requestAnimationFrame(() => {
      const a = document.activeElement;
      if (a && a !== document.body && a.isConnected) return; // the user already moved on
      document.querySelector<HTMLButtonElement>(REPLAY_BUTTON_SELECTOR)?.focus();
    });
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    e.preventDefault();
    e.stopPropagation();
    void exit();
  };

  const w = info.window;
  const ready = !r.loading && !r.error && w != null && r.startTs != null && r.endTs != null && r.cursorTs != null;
  const atEnd = ready && r.cursorTs! >= r.endTs!;
  const settledShown = atEnd && !info.partial && w?.result != null;

  return (
    <div
      ref={barRef}
      role="region"
      aria-label="Window replay"
      onKeyDown={onKeyDown}
      className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border bg-surface px-3 py-1.5 text-[12px]"
    >
      <ReplayBadge />
      <span className="num min-w-0 truncate text-text-2">
        {w ? etRange(w.startTs, w.endTs) : r.loading ? "Loading window…" : "—"}
      </span>
      {info.partial && ready && <span className="hidden shrink-0 text-[11px] text-muted md:inline">Live window · recorded so far</span>}

      <div className="order-last flex min-w-0 basis-full flex-wrap items-center gap-x-2 gap-y-1 sm:order-none sm:basis-auto sm:flex-1">
        {r.loading ? (
          <span role="status" className="inline-flex h-7 items-center gap-1.5 text-muted">
            <Loader2 size={13} className="animate-spin" /> {info.exiting ? "Returning to live…" : "Loading recorded data…"}
          </span>
        ) : r.error || !ready ? (
          <span className="inline-flex min-w-0 items-center gap-2">
            <span className="truncate text-down" role="alert">
              {r.error ?? "Replay data unavailable"}
            </span>
            {r.windowId && (
              <Button size="sm" variant="outline" onClick={() => void startReplay(r.windowId!)}>
                Retry
              </Button>
            )}
          </span>
        ) : (
          <>
            <Tip label={atEnd ? "Replay from the start" : r.playing ? "Pause" : "Play"}>
              <button
                type="button"
                onClick={toggleReplayPlaying}
                aria-label={atEnd ? "Replay from the start" : r.playing ? "Pause replay" : "Play replay"}
                className="touch-target inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-ctl border border-border-strong bg-surface-2 text-text hover:bg-[#1b2330]"
              >
                {atEnd ? <RotateCcw size={13} /> : r.playing ? <Pause size={13} /> : <Play size={13} className="translate-x-[1px]" />}
              </button>
            </Tip>
            <Segmented<ReplaySpeed>
              size="sm"
              ariaLabel="Replay speed"
              value={r.speed}
              onChange={setReplaySpeed}
              options={SPEED_OPTIONS}
              className="shrink-0"
            />
            <Scrubber
              className="order-last grow basis-full sm:order-none sm:basis-0"
              startTs={r.startTs!}
              endTs={r.endTs!}
              cursorTs={r.cursorTs!}
              windowEndTs={w!.endTs}
              frames={info.frames}
              fills={info.fills}
            />
            <span className="num shrink-0 whitespace-nowrap text-text-2" aria-live="off">
              <span className="hidden text-muted lg2:inline">{etTimeSec(r.cursorTs!)} · </span>
              <span className="text-text">{mmss(r.cursorTs! - r.startTs!)}</span>
              <span className="text-muted"> / {mmss(w!.endTs - w!.startTs)}</span>
            </span>
            {settledShown && (
              <span
                className={cn(
                  "num shrink-0 rounded-[6px] px-1.5 py-0.5 text-[11px] font-bold",
                  w!.result === "up" ? "bg-up-tint text-up" : "bg-down-tint text-down",
                )}
              >
                SETTLED {w!.result === "up" ? "UP" : "DOWN"}
              </span>
            )}
          </>
        )}
      </div>

      <Button size="sm" variant="outline" className="ml-auto shrink-0" onClick={() => void stopReplay()} aria-label="Exit replay">
        <X size={12} /> <span>Exit replay</span>
      </Button>
    </div>
  );
}
