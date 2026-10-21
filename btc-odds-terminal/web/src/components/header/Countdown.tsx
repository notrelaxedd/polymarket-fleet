"use client";
import { Timer } from "lucide-react";
import { useTerminal } from "@/lib/store";
import { useCountdownMs } from "@/lib/hooks";
import { mmss } from "@/lib/format";
import { cn } from "@/lib/utils";

export function Countdown() {
  const endTs = useTerminal((s) => s.current?.endTs ?? null);
  const ms = useCountdownMs(endTs, 250);
  const urgent = ms != null && ms <= 30_000;
  return (
    <div
      role="timer"
      aria-label="Time left in window"
      className={cn(
        "num flex h-8 items-center gap-1.5 rounded-full border border-ptb/40 bg-ptb-tint px-3 text-[15px] font-semibold text-ptb",
        urgent && "pulse-dot",
      )}
    >
      <Timer size={14} aria-hidden />
      {ms == null ? "--:--" : mmss(ms)}
    </div>
  );
}
