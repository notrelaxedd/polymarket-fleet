"use client";
import { Play } from "lucide-react";
import { Segmented } from "@/components/ui/segmented";
import { Chip } from "@/components/ui/chip";
import { Tip } from "@/components/ui/tooltip";
import { useTerminal, type Overlays, type Timeframe } from "@/lib/store";

const TF: { value: Timeframe; label: string }[] = [
  { value: 1, label: "1s" },
  { value: 5, label: "5s" },
  { value: 15, label: "15s" },
  { value: 60, label: "1m" },
  { value: 300, label: "5m" },
];

const CHIPS: { key: keyof Overlays; label: string; color: string; dashed?: boolean }[] = [
  { key: "upOdds", label: "Up odds", color: "#5B9CFF" },
  { key: "downOdds", label: "Down odds", color: "#F87171" },
  { key: "fair", label: "Fair value", color: "#C29BFF", dashed: true },
  { key: "ptb", label: "Price to beat", color: "#F5B942", dashed: true },
  { key: "windows", label: "Windows", color: "#A3ABB9" },
  { key: "edgePane", label: "Edge pane", color: "#5B9CFF" },
  { key: "perExchange", label: "Per-exchange", color: "#A3ABB9", dashed: true },
];

export function ChartToolbar() {
  const timeframe = useTerminal((s) => s.timeframe);
  const setTimeframe = useTerminal((s) => s.setTimeframe);
  const chartStyle = useTerminal((s) => s.chartStyle);
  const setChartStyle = useTerminal((s) => s.setChartStyle);
  const overlays = useTerminal((s) => s.overlays);
  const toggle = useTerminal((s) => s.toggleOverlay);

  return (
    <div className="flex items-center gap-3 overflow-x-auto border-b border-border bg-surface px-3 py-1.5">
      <Segmented ariaLabel="Timeframe" value={timeframe} onChange={setTimeframe} options={TF} />
      <Segmented
        ariaLabel="Chart style"
        value={chartStyle}
        onChange={setChartStyle}
        options={[
          { value: "candles", label: "Candles" },
          { value: "line", label: "Line" },
        ]}
      />
      <div aria-hidden className="h-5 w-px shrink-0 bg-border-strong" />
      <div className="flex items-center gap-1.5" role="group" aria-label="Overlays">
        {CHIPS.map((c) => (
          <Chip key={c.key} on={overlays[c.key]} swatch={c.color} dashed={c.dashed} onClick={() => toggle(c.key)}>
            {c.label}
          </Chip>
        ))}
      </div>
      <div className="min-w-2 flex-1" />
      <Tip label="Step 6">
        <span tabIndex={0} className="inline-flex shrink-0">
          <button
            type="button"
            disabled
            aria-label="Replay window (available in step 6)"
            className="touch-target inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-ctl border border-border-strong px-2.5 text-[12px] font-medium text-text-2 disabled:opacity-50"
          >
            <Play size={12} /> Replay window
          </button>
        </span>
      </Tip>
    </div>
  );
}
