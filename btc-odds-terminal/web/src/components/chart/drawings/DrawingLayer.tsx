"use client";
// Drawing tools + stickers on the main chart.
//  - DrawingsPrimitive (BTC scale) paints every drawing; DrawingsAxisPrimitive (odds scale) adds left-axis tags
//  - DrawingController turns pointer / touch / keyboard input into edits (see controller.ts)
//  - DrawingToolbar (recolor · scale · text · alert · lock · delete) and TextEditor are HTML overlays
// Drawings live in store.drawings (anchored to time + price); edits go through ./actions, which keeps the
// undo/redo stacks and the debounced /api/drawings save queue (./sync).

import * as React from "react";
import { cn } from "@/lib/utils";
import { useChart } from "../context";
import { dismissDrawingSaveError, drawingSync, redoDrawings, retryDrawingSave, undoDrawings } from "./actions";
import { requestClearDrawings } from "./ClearDrawingsDialog";
import { DrawingController } from "./controller";
import { DrawingToolbar } from "./DrawingToolbar";
import { DrawingsAxisPrimitive, DrawingsPrimitive } from "./DrawingsPrimitive";
import { TextEditor } from "./TextEditor";
import { useDrawingUi } from "./ui";

export function DrawingLayer() {
  const h = useChart();
  const [ctl, setCtl] = React.useState<DrawingController | null>(null);

  React.useEffect(() => {
    if (!h) return;
    drawingSync();
    const prim = new DrawingsPrimitive(h);
    const axis = new DrawingsAxisPrimitive(h);
    const c = new DrawingController(h, prim, axis);
    const detachPrim = h.attach(prim, "btc");
    const detachAxis = h.attach(axis, "odds");
    c.attach();
    setCtl(c);
    // re-measure canvas text once the web fonts are in
    let alive = true;
    document.fonts?.ready.then(() => alive && c.redraw());
    return () => {
      alive = false;
      setCtl(null);
      c.detach();
      useDrawingUi.getState().setEditing(null);
      detachAxis();
      detachPrim();
    };
  }, [h]);

  // legacy window event API: { action: "undo" | "redo" | "clear" }
  React.useEffect(() => {
    const f = (e: Event) => {
      const a = (e as CustomEvent<{ action?: string }>).detail?.action;
      if (a === "undo") undoDrawings();
      else if (a === "redo") redoDrawings();
      else if (a === "clear") requestClearDrawings();
    };
    window.addEventListener("terminal:drawing", f);
    return () => window.removeEventListener("terminal:drawing", f);
  }, []);

  if (!ctl) return null;
  return (
    <>
      <DrawingToolbar ctl={ctl} />
      <TextEditor ctl={ctl} />
      <SaveStatus />
    </>
  );
}

/** persistent "not saved" state of the drawing save queue (notices auto-dismiss; this stays until fixed) */
function SaveStatus() {
  const status = useDrawingUi((s) => s.saveStatus);
  const error = useDrawingUi((s) => s.saveError);
  if (status !== "retrying" && status !== "offline" && status !== "rejected") return null;
  const text = status === "retrying" ? "Drawings not saved · retrying" : status === "offline" ? "Drawings not saved" : "Drawing change rejected";
  return (
    <div
      data-drawing-ui=""
      role="status"
      title={error ?? undefined}
      className={cn(
        "absolute left-1/2 top-2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-ctl border bg-surface-2 px-2 py-1 shadow-[0_6px_20px_rgba(0,0,0,0.45)]",
        status === "retrying" ? "border-ptb/40 text-ptb" : "border-down/40 text-down",
      )}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <span className="text-[11px] font-medium uppercase leading-[14px] tracking-wide">{text}</span>
      {status === "offline" && (
        <button type="button" className="text-[11px] font-medium uppercase leading-[14px] tracking-wide text-text hover:underline" onClick={retryDrawingSave}>
          Retry
        </button>
      )}
      {status === "rejected" && (
        <button type="button" aria-label="Dismiss" className="text-[11px] font-medium uppercase leading-[14px] tracking-wide text-muted hover:text-text" onClick={dismissDrawingSaveError}>
          Dismiss
        </button>
      )}
    </div>
  );
}
