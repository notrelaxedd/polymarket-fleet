"use client";
import { Tip } from "@/components/ui/tooltip";
import { useTerminal } from "@/lib/store";
import { etRange, btc } from "@/lib/format";
import { cn } from "@/lib/utils";

export function LastResults() {
  const recent = useTerminal((s) => s.recent);
  // newest on the right
  const last = recent.filter((w) => w.result).slice(0, 8).reverse();
  return (
    <div className="flex flex-col gap-0.5">
      <span className="label whitespace-nowrap">Last 8</span>
      <div className="flex h-[18px] items-center gap-1" role="list" aria-label="Last 8 window results">
        {last.length === 0 && <span className="num text-[12px] text-muted">—</span>}
        {last.map((w) => (
          <Tip
            key={w.id}
            label={
              <span className="num">
                {etRange(w.startTs, w.endTs)} · {w.result === "up" ? "UP" : "DOWN"}
                {w.settlePrice != null && <> · ${btc(w.settlePrice)}</>}
              </span>
            }
          >
            <span
              role="listitem"
              tabIndex={0}
              className={cn(
                "num flex h-[18px] w-[18px] items-center justify-center rounded-[4px] text-[11px] font-bold",
                w.result === "up" ? "bg-up-tint text-up" : "bg-down-tint text-down",
              )}
            >
              {w.result === "up" ? "U" : "D"}
            </span>
          </Tip>
        ))}
      </div>
    </div>
  );
}
