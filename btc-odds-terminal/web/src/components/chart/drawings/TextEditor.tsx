"use client";
// Inline editor for a text note (multi-line card) or a horizontal line's label (single line), placed over
// the drawing on the chart. Enter commits (Shift+Enter = new line in a note), Esc cancels, blur commits.
// A new note is only added to the drawing list when it is committed with some text.

import * as React from "react";
import { useTerminal } from "@/lib/store";
import { addDrawing, deleteDrawing, updateDrawing } from "./actions";
import type { DrawingController, SelectionLayout } from "./controller";
import { useDrawingUi } from "./ui";

const MAX_LEN = 500;

export function TextEditor({ ctl }: { ctl: DrawingController }) {
  const editing = useDrawingUi((s) => s.editing);
  const stored = useTerminal((s) => (editing ? s.drawings.find((d) => d.id === editing.id) : undefined));
  const d = editing?.draft ?? stored;
  const boxRef = React.useRef<HTMLDivElement>(null);
  const fieldRef = React.useRef<HTMLTextAreaElement & HTMLInputElement>(null);
  const [value, setValue] = React.useState("");
  const done = React.useRef(false);
  const id = editing?.id ?? null;

  React.useEffect(() => {
    if (!id) return;
    done.current = false;
    setValue(useDrawingUi.getState().editing?.draft?.label ?? useTerminal.getState().drawings.find((x) => x.id === id)?.label ?? "");
    const t = setTimeout(() => {
      fieldRef.current?.focus();
      fieldRef.current?.select();
    }, 0);
    return () => clearTimeout(t);
  }, [id]);

  React.useEffect(() => {
    if (!id) return;
    return ctl.onLayout((l: SelectionLayout | null) => {
      const el = boxRef.current;
      if (!el) return;
      if (!l || l.d.id !== id) {
        el.style.visibility = "hidden";
        return;
      }
      el.style.visibility = "visible";
      const host = el.offsetParent as HTMLElement | null;
      const hostW = host?.clientWidth ?? 0;
      const w = el.offsetWidth;
      let left = l.paneLeft + l.box.left;
      let top = l.box.top;
      if (l.d.kind === "hline") {
        left = l.paneLeft + l.box.right - w;
        top = (l.box.top + l.box.bottom) / 2 - el.offsetHeight / 2;
      }
      left = Math.max(4, Math.min(hostW - w - 4, left));
      el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
    });
  }, [ctl, id]);

  // the drawing went away while its editor was open (deleted / replaced by another tab, undo): close the
  // editor, or the stale `editing` state would hide the toolbar and the canvas text of that id
  const missing = !!editing && !d;
  React.useEffect(() => {
    if (!missing) return;
    const ed = useDrawingUi.getState().editing;
    if (ed && !ed.draft && !useTerminal.getState().drawings.some((x) => x.id === ed.id)) useDrawingUi.getState().setEditing(null);
  }, [missing]);

  if (!editing || !d) return null;

  const close = () => useDrawingUi.getState().setEditing(null);
  const commit = () => {
    if (done.current) return;
    done.current = true;
    const text = value.replace(/\s+$/, "").slice(0, MAX_LEN);
    const label = text.trim() ? text : null;
    if (editing.draft) {
      if (label) addDrawing({ ...editing.draft, label }, true);
      else useTerminal.getState().selectDrawing(null);
    } else if (d.kind === "text" && !label) {
      deleteDrawing(d.id);
    } else if ((d.label ?? null) !== label) {
      updateDrawing(d.id, (x) => ({ ...x, label }));
    }
    close();
  };
  const cancel = () => {
    if (done.current) return;
    done.current = true;
    if (editing.draft) useTerminal.getState().selectDrawing(null);
    close();
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    } else if (e.key === "Enter" && !(d.kind === "text" && e.shiftKey)) {
      e.preventDefault();
      commit();
    }
  };

  const common = {
    ref: fieldRef,
    value,
    maxLength: MAX_LEN,
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement | HTMLInputElement>) => setValue(e.target.value),
    onKeyDown,
    onBlur: commit,
    spellCheck: false,
    style: { color: d.color, borderColor: d.color },
  };

  return (
    <div
      ref={boxRef}
      data-drawing-ui=""
      className="absolute left-0 top-0 z-30"
      style={{ visibility: "hidden", willChange: "transform" }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      {d.kind === "text" ? (
        <textarea
          {...common}
          aria-label="Note text"
          placeholder="Note"
          rows={Math.min(8, Math.max(2, value.split("\n").length))}
          className="block w-[236px] resize-none rounded-[6px] border bg-surface-2 px-2 py-1.5 font-sans text-[12px] leading-4 shadow-[0_6px_20px_rgba(0,0,0,0.45)] outline-none placeholder:text-label"
        />
      ) : (
        <input
          {...common}
          aria-label="Line label"
          placeholder="Label"
          className="block h-7 w-[200px] rounded-[6px] border bg-surface-2 px-2 font-sans text-[12px] shadow-[0_6px_20px_rgba(0,0,0,0.45)] outline-none placeholder:text-label"
        />
      )}
      <div className="mt-1 inline-block rounded-[4px] bg-surface-2/95 px-1.5 py-0.5 text-[11px] text-muted">
        {d.kind === "text" ? "Enter to save · Shift+Enter new line · Esc cancel" : "Enter to save · Esc cancel"}
      </div>
    </div>
  );
}
