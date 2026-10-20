"use client";
import { Settings as Cog } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { useTerminal } from "@/lib/store";

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-6 py-1.5">
      <span className="text-muted">{k}</span>
      <span className="num text-text">{children}</span>
    </div>
  );
}

export function ModeBadge() {
  const mode = useTerminal((s) => s.settings?.mode ?? "paper");
  return mode === "live" ? (
    <span className="num rounded-ctl border border-down/50 bg-down-tint px-2.5 py-1 text-[12px] font-bold tracking-wider text-down">LIVE</span>
  ) : (
    <span className="num rounded-ctl border border-ptb/40 bg-ptb-tint px-2.5 py-1 text-[12px] font-bold tracking-wider text-ptb">PAPER</span>
  );
}

export function SettingsMenu() {
  const s = useTerminal((st) => st.settings);
  return (
    <Popover>
      <Tip label="Settings">
        <PopoverTrigger asChild>
          <Button variant="ghost" size="icon" aria-label="Settings" className="border border-border">
            <Cog size={16} />
          </Button>
        </PopoverTrigger>
      </Tip>
      <PopoverContent align="end" className="w-[300px]">
        <div className="label pb-1">Settings</div>
        <div className="divide-y divide-border">
          <Row k="Mode">{s ? s.mode.toUpperCase() : "—"}</Row>
          <Row k="Vol lookback">{s ? `${s.volLookbackMin} min` : "—"}</Row>
          <Row k="Fee (¢ / contract)">{s ? (s.feeCentsPerContract == null ? "[FEE]" : `${s.feeCentsPerContract}¢`) : "—"}</Row>
        </div>
        {s?.feeNote && <p className="mt-2 text-[12px] text-muted">{s.feeNote}</p>}
        <p className="mt-2 rounded-ctl bg-inset px-2 py-1.5 text-[12px] text-muted">Read-only for now — editable in step 6.</p>
      </PopoverContent>
    </Popover>
  );
}
