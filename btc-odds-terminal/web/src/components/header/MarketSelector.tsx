"use client";
import * as React from "react";
import { ChevronDown } from "lucide-react";
import type { MarketWindow } from "@shared/protocol";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useTerminal } from "@/lib/store";
import { etRange, btc } from "@/lib/format";
import { cn } from "@/lib/utils";

function Row({ w, tag, tagClass }: { w: MarketWindow; tag: string; tagClass: string }) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-ctl px-2 py-1.5 hover:bg-[#1b2330]">
      <div className="min-w-0">
        <div className="num text-[13px] text-text">{etRange(w.startTs, w.endTs)}</div>
        <div className="num text-[11px] text-muted">PTB {w.priceToBeat != null ? `$${btc(w.priceToBeat)}` : "—"}</div>
      </div>
      <span className={cn("rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide", tagClass)}>{tag}</span>
    </div>
  );
}

export function MarketSelector() {
  const current = useTerminal((s) => s.current);
  const next = useTerminal((s) => s.next);
  const recent = useTerminal((s) => s.recent);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Select market window"
          className="touch-target group flex min-w-0 items-center gap-2.5 rounded-ctl border border-border-strong bg-inset px-3 py-1 text-left transition-colors hover:bg-surface-2"
        >
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="whitespace-nowrap text-[13px] font-semibold text-text">BTC Up or Down · 15m</span>
            <span className="num whitespace-nowrap text-[12px] text-muted">
              {current ? etRange(current.startTs, current.endTs) : "—"}
            </span>
          </span>
          <ChevronDown size={16} className="shrink-0 text-muted transition-transform group-data-[state=open]:rotate-180" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[300px] p-2">
        <div className="label px-2 pb-1 pt-0.5">Windows</div>
        {current && <Row w={current} tag="Live" tagClass="bg-ptb-tint text-ptb" />}
        {next && <Row w={next} tag="Next" tagClass="bg-odds-tint text-odds-text" />}
        {recent.slice(0, 6).map((w) => (
          <Row
            key={w.id}
            w={w}
            tag={w.result ? `Settled ${w.result.toUpperCase()}` : "Closed"}
            tagClass={w.result === "up" ? "bg-up-tint text-up" : w.result === "down" ? "bg-down-tint text-down" : "bg-surface text-muted"}
          />
        ))}
        {!current && !next && recent.length === 0 && <div className="px-2 py-3 text-muted">No windows yet.</div>}
        <div className="mt-1 border-t border-border px-2 pt-2 text-[11px] text-muted">Window switching arrives with replay (step 6).</div>
      </PopoverContent>
    </Popover>
  );
}
