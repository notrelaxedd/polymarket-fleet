"use client";
// Small floating toolbar above the selected drawing: recolor (token swatches), BTC/odds anchor scale,
// edit text / label, add alert (horizontal lines), lock, delete. Follows the drawing while the chart pans
// (positioned imperatively from the controller's layout callback, no re-render per frame).

import * as React from "react";
import { Bell, Lock, LockOpen, Pencil, Trash2 } from "lucide-react";
import type { Drawing } from "@shared/protocol";
import { Tip } from "@/components/ui/tooltip";
import { useTerminal } from "@/lib/store";
import { cn } from "@/lib/utils";
import { addAlertFromSelection, deleteDrawing, updateDrawing } from "./actions";
import type { DrawingController, SelectionLayout } from "./controller";
import { KIND_LABELS, PALETTE } from "./model";
import { useDrawingUi } from "./ui";

const GAP = 10;
const EDGE = 4;

const btn =
  "touch-target flex h-7 w-7 shrink-0 items-center justify-center rounded-[6px] text-muted transition-colors hover:bg-[#1b2330] hover:text-text disabled:pointer-events-none disabled:opacity-40";

export function DrawingToolbar({ ctl }: { ctl: DrawingController }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const layoutRef = React.useRef<SelectionLayout | null>(null);
  const [id, setId] = React.useState<string | null>(null);
  const d = useTerminal((s) => (id ? s.drawings.find((x) => x.id === id) : undefined));
  const globalLock = useTerminal((s) => s.drawingsLocked);
  const selectedId = useTerminal((s) => s.selectedDrawingId);
  const editorOpen = useDrawingUi((s) => s.editing != null);
  const alertBusy = useDrawingUi((s) => s.alertBusy);

  const place = React.useCallback(() => {
    const el = ref.current;
    const l = layoutRef.current;
    if (!el || !l) return;
    const host = el.offsetParent as HTMLElement | null;
    const hostW = host?.clientWidth ?? 0;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const cx = l.paneLeft + (l.box.left + l.box.right) / 2;
    let left = cx - w / 2;
    left = Math.max(EDGE, Math.min(hostW - w - EDGE, left));
    let top = l.box.top - GAP - h;
    if (top < EDGE) top = l.box.bottom + GAP;
    el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }, []);

  React.useEffect(
    () =>
      ctl.onLayout((l) => {
        layoutRef.current = l;
        setId(l ? l.d.id : null);
        place();
      }),
    [ctl, place],
  );
  // first placement once the toolbar for a new selection has rendered (its size is known)
  React.useLayoutEffect(() => place(), [id, d, place]);

  if (!id || !d || selectedId !== id || editorOpen) return null;
  const locked = d.locked || globalLock;
  const set = (patch: Partial<Drawing>) => updateDrawing(d.id, (x) => ({ ...x, ...patch }));
  const scale = d.points[0]?.scale ?? "btc";

  return (
    <div
      ref={ref}
      data-drawing-ui=""
      role="toolbar"
      aria-label={`${KIND_LABELS[d.kind]} options`}
      className="absolute left-0 top-0 z-20 flex max-w-[calc(100%-8px)] flex-wrap items-center gap-0.5 rounded-ctl border border-border-strong bg-surface-2 p-1 shadow-[0_6px_20px_rgba(0,0,0,0.45)]"
      style={{ willChange: "transform" }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {d.kind !== "sticker" && d.kind !== "measure" && (
        <>
          {PALETTE.map((p) => {
            const on = d.color.toLowerCase() === p.color.toLowerCase();
            return (
              <Tip key={p.color} label={p.label} side="top">
                <button
                  type="button"
                  aria-label={`Color ${p.label}`}
                  aria-pressed={on}
                  disabled={locked}
                  className={cn(btn, "w-6")}
                  onClick={() => set({ color: p.color })}
                >
                  <span
                    aria-hidden
                    className={cn("block h-3.5 w-3.5 rounded-full", on && "ring-2 ring-offset-2 ring-offset-surface-2")}
                    style={{ background: p.color, ["--tw-ring-color" as string]: p.color }}
                  />
                </button>
              </Tip>
            );
          })}
          <Divider />
        </>
      )}
      {d.kind !== "vline" && (
        <Tip label={scale === "btc" ? "Anchored to BTC price · switch to Up odds" : "Anchored to Up odds · switch to BTC price"} side="top">
          <button
            type="button"
            aria-label={scale === "btc" ? "Anchor to Up odds scale" : "Anchor to BTC price scale"}
            disabled={locked}
            className={cn(btn, "w-auto px-1.5 font-mono text-[11px] font-semibold tracking-wide", scale === "odds" ? "text-odds-text" : "text-text-2")}
            onClick={() => {
              const next = ctl.convertScale(d, scale === "btc" ? "odds" : "btc");
              if (next) updateDrawing(d.id, () => next);
            }}
          >
            {scale === "btc" ? "BTC" : "ODDS"}
          </button>
        </Tip>
      )}
      {(d.kind === "text" || d.kind === "hline") && (
        <Tip label={d.kind === "text" ? "Edit text" : "Edit label"} side="top">
          <button type="button" aria-label={d.kind === "text" ? "Edit text" : "Edit label"} disabled={locked} className={btn} onClick={() => ctl.edit(d.id)}>
            <Pencil size={14} />
          </button>
        </Tip>
      )}
      {d.kind === "hline" && (
        <Tip label="Alert at this line" side="top">
          <button type="button" aria-label="Create alert at this line" disabled={alertBusy} className={btn} onClick={() => void addAlertFromSelection(d)}>
            <Bell size={14} />
          </button>
        </Tip>
      )}
      <Divider />
      <Tip label={globalLock ? "All drawings are locked (tools rail)" : d.locked ? "Unlock" : "Lock"} side="top">
        <button
          type="button"
          aria-label={d.locked ? "Unlock drawing" : "Lock drawing"}
          aria-pressed={d.locked}
          disabled={globalLock}
          className={cn(btn, (d.locked || globalLock) && "text-ptb hover:text-ptb")}
          onClick={() => set({ locked: !d.locked })}
        >
          {d.locked || globalLock ? <Lock size={14} /> : <LockOpen size={14} />}
        </button>
      </Tip>
      <Tip label={locked ? "Unlock to delete" : "Delete (Del)"} side="top">
        <button type="button" aria-label="Delete drawing" disabled={locked} className={cn(btn, "hover:!text-down")} onClick={() => deleteDrawing(d.id)}>
          <Trash2 size={14} />
        </button>
      </Tip>
    </div>
  );
}

function Divider() {
  return <span aria-hidden className="mx-0.5 h-4 w-px shrink-0 bg-border-strong" />;
}
