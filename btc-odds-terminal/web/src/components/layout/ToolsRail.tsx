"use client";
import * as React from "react";
import { Bell, Lock, LockOpen, MousePointer2, Sticker as StickerIcon, Trash2, Type, Undo2 } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Sticker, STICKER_IDS, STICKERS, type StickerId } from "@/components/stickers";
import { addAlertFromSelection, undoDrawings } from "@/components/chart/drawings/actions";
import { ClearDrawingsDialog, requestClearDrawings } from "@/components/chart/drawings/ClearDrawingsDialog";
import { useDrawingUi } from "@/components/chart/drawings/ui";
import { useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import { FibIcon, HLineIcon, MeasureIcon, RayIcon, RectIcon, TrendLineIcon, VLineIcon } from "./tool-icons";

/** activeTool ids written to the store: cursor | trend | ray | hline | vline | rect | fib | text | measure | sticker:<StickerId> */
const TOOLS: { id: string; label: string; icon: React.ReactNode }[] = [
  { id: "cursor", label: "Cursor", icon: <MousePointer2 size={18} /> },
  { id: "trend", label: "Trend line", icon: <TrendLineIcon /> },
  { id: "ray", label: "Ray", icon: <RayIcon /> },
  { id: "hline", label: "Horizontal line", icon: <HLineIcon /> },
  { id: "vline", label: "Vertical line", icon: <VLineIcon /> },
  { id: "rect", label: "Rectangle", icon: <RectIcon /> },
  { id: "fib", label: "Fib retracement", icon: <FibIcon /> },
  { id: "text", label: "Text note", icon: <Type size={18} /> },
  { id: "measure", label: "Price-range measure", icon: <MeasureIcon /> },
];

const btn =
  "touch-target flex h-9 w-9 shrink-0 items-center justify-center rounded-ctl text-muted transition-colors hover:bg-surface-2 hover:text-text";
const btnActive = "!bg-odds-tint !text-odds-text shadow-[inset_0_0_0_1px_rgba(91,156,255,0.45)]";

export function ToolsRail() {
  const activeTool = useTerminal((s) => s.activeTool);
  const setActiveTool = useTerminal((s) => s.setActiveTool);
  const locked = useTerminal((s) => s.drawingsLocked);
  const setLocked = useTerminal((s) => s.setDrawingsLocked);
  const canUndo = useDrawingUi((s) => s.canUndo);
  const alertBusy = useDrawingUi((s) => s.alertBusy);
  const hasDrawings = useTerminal((s) => s.drawings.length > 0);
  const selectedHline = useTerminal((s) => s.drawings.find((d) => d.id === s.selectedDrawingId)?.kind === "hline");
  const [pickerOpen, setPickerOpen] = React.useState(false);
  const stickerActive = activeTool.startsWith("sticker");
  const [side, setSide] = React.useState<"right" | "bottom">("right");

  React.useEffect(() => {
    const mq = window.matchMedia("(min-width: 640px)");
    const f = () => setSide(mq.matches ? "right" : "bottom");
    f();
    mq.addEventListener("change", f);
    return () => mq.removeEventListener("change", f);
  }, []);

  const tipSide = side === "right" ? "right" : "bottom";

  return (
    <nav
      aria-label="Drawing tools"
      className="flex h-full shrink-0 items-center gap-1 overflow-x-auto border-b border-border bg-surface px-2 py-1.5 sm:flex-col sm:overflow-visible sm:border-b-0 sm:border-r sm:px-0 sm:py-2"
    >
      {TOOLS.map((t) => (
        <Tip key={t.id} label={t.label} side={tipSide}>
          <button
            type="button"
            aria-label={t.label}
            aria-pressed={activeTool === t.id}
            onClick={() => setActiveTool(t.id)}
            className={cn(btn, activeTool === t.id && btnActive)}
          >
            {t.icon}
          </button>
        </Tip>
      ))}

      <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
        <Tip label="Stickers" side={tipSide} disabled={pickerOpen}>
          <PopoverTrigger asChild>
            <button type="button" aria-label="Stickers" aria-pressed={stickerActive} className={cn(btn, stickerActive && btnActive)}>
              <StickerIcon size={18} />
            </button>
          </PopoverTrigger>
        </Tip>
        <PopoverContent side={side} align="start" sideOffset={10} className="w-[232px] p-3">
          <div className="label pb-2">Stickers</div>
          <div className="grid grid-cols-4 gap-2">
            {STICKER_IDS.map((id: StickerId) => {
              const on = activeTool === `sticker:${id}`;
              return (
                <button
                  key={id}
                  type="button"
                  aria-label={STICKERS[id].label}
                  aria-pressed={on}
                  title={STICKERS[id].label}
                  onClick={() => {
                    setActiveTool(`sticker:${id}`);
                    setPickerOpen(false);
                  }}
                  className={cn(
                    "touch-target flex h-12 w-full items-center justify-center rounded-ctl transition-colors hover:bg-[#1b2330]",
                    on && "bg-odds-tint shadow-[inset_0_0_0_1px_rgba(91,156,255,0.5)]",
                  )}
                >
                  <Sticker id={id} size={34} />
                </button>
              );
            })}
          </div>
          <p className="mt-3 text-[12px] leading-4 text-muted">Click the chart to place · drag to move</p>
        </PopoverContent>
      </Popover>

      <div aria-hidden className="mx-1 h-6 w-px shrink-0 bg-border-strong sm:mx-0 sm:my-1 sm:h-px sm:w-6" />

      <Tip label={selectedHline ? "Add alert at the selected line" : "Add alert"} side={tipSide}>
        <button
          type="button"
          aria-label="Add alert"
          disabled={alertBusy}
          className={cn(btn, alertBusy && "opacity-40")}
          onClick={() => void addAlertFromSelection()}
        >
          <Bell size={18} />
        </button>
      </Tip>
      <Tip label={locked ? "Unlock drawings" : "Lock drawings"} side={tipSide}>
        <button
          type="button"
          aria-label="Lock drawings"
          aria-pressed={locked}
          className={cn(btn, locked && btnActive)}
          onClick={() => setLocked(!locked)}
        >
          {locked ? <Lock size={18} /> : <LockOpen size={18} />}
        </button>
      </Tip>
      <Tip label="Undo (Ctrl/Cmd+Z)" side={tipSide}>
        <button
          type="button"
          aria-label="Undo"
          aria-disabled={!canUndo}
          className={cn(btn, !canUndo && "opacity-40 hover:bg-transparent hover:text-muted")}
          onClick={() => undoDrawings()}
        >
          <Undo2 size={18} />
        </button>
      </Tip>
      <Tip label="Delete all drawings" side={tipSide}>
        <button
          type="button"
          aria-label="Delete all drawings"
          aria-disabled={!hasDrawings}
          className={cn(btn, hasDrawings ? "hover:!text-down" : "opacity-40 hover:bg-transparent hover:text-muted")}
          onClick={requestClearDrawings}
        >
          <Trash2 size={18} />
        </button>
      </Tip>
      <ClearDrawingsDialog />
    </nav>
  );
}
