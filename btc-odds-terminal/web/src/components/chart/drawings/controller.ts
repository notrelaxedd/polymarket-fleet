// Pointer / touch / keyboard interaction for chart drawings.
//
// Input never fights the chart's own pan & zoom: pointerdown is examined at the window's bubble phase
// (after the chart and any other overlay, e.g. position lines, which claim theirs with preventDefault) and
// is only claimed when a drawing tool is active or a draggable drawing is under the pointer. Claiming
// calls preventDefault on the pointerdown — which suppresses the compatibility mouse events lightweight-charts
// pans with — and stops the matching touchstart before it reaches the chart (and the page scroll).
// With the cursor tool and nothing grabbed, every event passes through untouched. A press inside the
// filled area of an unselected rectangle / fib / measure is not claimed either (the chart pans); a click
// there (no movement) selects it.
//
// Keyboard: Delete / Backspace / Ctrl+Z only act while focus is on the chart, the page body or the
// drawing toolbar, and the selection is dropped when a press or focus goes to another control.
//
// Creation: 1-point tools (horizontal / vertical line, text, sticker) place on press and can be dragged
// before release; 2-point tools draw by press-drag-release or by click … click (live preview between).
// After a drawing is created the tool returns to the cursor and the new drawing is selected.

import type { Drawing, DrawingPoint, DrawingScale } from "@shared/protocol";
import { useTerminal } from "@/lib/store";
import type { ChartHandle } from "../context";
import {
  addDrawing,
  canRedoDrawings,
  canUndoDrawings,
  deleteSelected,
  drawingsReady,
  onDrawingRemap,
  redoDrawings,
  undoDrawings,
  updateDrawing,
} from "./actions";
import type { DrawingScene, DrawingsAxisPrimitive, DrawingsPrimitive } from "./DrawingsPrimitive";
import { moveBody, moveHandle, type Box, type Part, type XY } from "./geometry";
import { makePoint, newDrawing, parseTool, pointCount, sameInput, type ToolSpec } from "./model";
import { useDrawingUi } from "./ui";

type Gesture =
  | {
      k: "create";
      pointerId: number;
      start: XY;
      moved: boolean;
      draft: Drawing;
    }
  | {
      k: "drag";
      pointerId: number;
      part: Part;
      start: Drawing;
      /** pointer position at press, in the drawing's data space */
      anchor: { time: number; price: number };
      /** pointer offset from the grabbed handle (px) */
      offset: XY;
      startXY: XY;
      moved: boolean;
      isNew: boolean;
    };

export interface SelectionLayout {
  d: Drawing;
  /** selected drawing's box in pane px */
  box: Box;
  /** pane origin inside the chart container (left price-scale width) */
  paneLeft: number;
}

/** elements that keep the drawing selection when pressed / focused (toolbar, inline editor, tools rail) */
const KEEP_SELECTION = '[data-drawing-ui], nav[aria-label="Drawing tools"]';

const MOVE_SLOP_MOUSE = 3;
const MOVE_SLOP_TOUCH = 8;
const TOL_MOUSE = 6;
const TOL_TOUCH = 14;

/** inside another chart overlay (position-line labels): its own handlers own pointer and key events there */
export function inChartOverlay(t: EventTarget | null): boolean {
  return typeof Element !== "undefined" && t instanceof Element && t.closest("[data-chart-overlay]") != null;
}

export function isEditableTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  if (t.isContentEditable) return true;
  const tag = t.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = (t as HTMLInputElement).type;
  return !["button", "checkbox", "radio", "range", "submit", "reset", "color"].includes(type);
}

export class DrawingController {
  private gesture: Gesture | null = null;
  /** 2-point drawing waiting for its second click */
  private pending: Drawing | null = null;
  /** live preview of the drawing being created / dragged (replaces or extends the store list) */
  private override: Drawing | null = null;
  private hoverId: string | null = null;
  private claimedTouch = false;
  /** press inside an unselected drawing's fill: selected on release if the pointer did not move */
  private clickSelect: { id: string; pointerId: number; at: { clientX: number; clientY: number }; touch: boolean } | null = null;
  private layoutListeners = new Set<(l: SelectionLayout | null) => void>();
  private lastLayout: SelectionLayout | null = null;
  private cleanup: (() => void)[] = [];

  constructor(
    private readonly h: ChartHandle,
    private readonly prim: DrawingsPrimitive,
    private readonly axis: DrawingsAxisPrimitive,
  ) {
    prim.getScene = () => this.scene();
    axis.getScene = () => this.scene();
    prim.onLayout = (sel) => this.emitLayout(sel);
  }

  // ---- lifecycle ------------------------------------------------------------------------------------

  attach() {
    const host = this.h.container;
    const on = <K extends keyof WindowEventMap>(t: Window, type: K, f: (e: WindowEventMap[K]) => void, opts?: AddEventListenerOptions) => {
      t.addEventListener(type, f as EventListener, opts);
      this.cleanup.push(() => t.removeEventListener(type, f as EventListener, opts));
    };
    const onEl = <K extends keyof HTMLElementEventMap>(type: K, f: (e: HTMLElementEventMap[K]) => void, opts?: AddEventListenerOptions) => {
      host.addEventListener(type, f as EventListener, opts);
      this.cleanup.push(() => host.removeEventListener(type, f as EventListener, opts));
    };
    on(window, "pointerdown", this.onDown);
    on(window, "pointermove", this.onMove);
    on(window, "pointerup", this.onUp);
    on(window, "pointercancel", this.onCancel);
    on(window, "keydown", this.onKey);
    on(window, "blur", this.onBlur);
    on(window, "focusin", this.onFocusIn);
    onEl("touchstart", this.onTouchStart, { capture: true, passive: false });
    onEl("dblclick", this.onDblClick, { capture: true });
    onEl("pointerleave", this.onLeave);
    this.cleanup.push(
      useTerminal.subscribe((s, p) => {
        if (s.activeTool !== p.activeTool) {
          if (this.pending || this.gesture?.k === "create") this.cancelGesture();
        }
        if (
          s.drawings !== p.drawings ||
          s.selectedDrawingId !== p.selectedDrawingId ||
          s.drawingsLocked !== p.drawingsLocked ||
          s.activeTool !== p.activeTool
        )
          this.redraw();
      }),
      useDrawingUi.subscribe((s, p) => {
        if (s.editing !== p.editing) this.redraw();
      }),
      onDrawingRemap((from, to) => this.remap(from, to)),
    );
    this.redraw();
  }

  detach() {
    for (const f of this.cleanup.splice(0)) f();
    this.prim.onLayout = null;
    this.layoutListeners.clear();
    this.gesture = null;
    this.pending = null;
    this.override = null;
    this.clickSelect = null;
    // an open inline editor belongs to this chart: a new text note's draft is discarded with it
    if (useDrawingUi.getState().editing) useDrawingUi.getState().setEditing(null);
  }

  /** a just-created drawing got its server id: keep an in-progress drag / preview pointed at it */
  private remap(from: string, to: string) {
    const g = this.gesture;
    if (g?.k === "drag" && g.start.id === from) g.start = { ...g.start, id: to };
    if (this.override?.id === from) this.override = { ...this.override, id: to };
    if (this.hoverId === from) this.hoverId = to;
  }

  /** subscribe to the selected drawing's screen box (null = nothing selected / visible / mid-gesture) */
  onLayout(fn: (l: SelectionLayout | null) => void): () => void {
    this.layoutListeners.add(fn);
    fn(this.lastLayout);
    return () => this.layoutListeners.delete(fn);
  }

  /** true while a drawing is being created or dragged (the toolbar hides) */
  busy(): boolean {
    return this.gesture != null || this.pending != null;
  }

  redraw() {
    this.prim.update();
    this.axis.update();
  }

  // ---- scene ----------------------------------------------------------------------------------------

  scene(): DrawingScene {
    const s = useTerminal.getState();
    const ui = useDrawingUi.getState();
    let drawings: readonly Drawing[] = s.drawings;
    const extra = this.override ?? ui.editing?.draft ?? null;
    if (extra) {
      const i = drawings.findIndex((d) => d.id === extra.id);
      drawings = i >= 0 ? drawings.map((d, j) => (j === i ? extra : d)) : [...drawings, extra];
    }
    return {
      drawings,
      selectedId: s.selectedDrawingId,
      hoverId: this.gesture ? null : this.hoverId,
      editingId: ui.editing?.id ?? null,
      locked: s.drawingsLocked,
      interactive: !parseTool(s.activeTool) && !this.gesture,
    };
  }

  private emitLayout(sel: { d: Drawing; box: Box } | null) {
    const l = sel && !this.busy() ? { ...sel, paneLeft: this.prim.coords.paneLeft() } : null;
    const prev = this.lastLayout;
    if (
      prev === l ||
      (prev &&
        l &&
        prev.d === l.d &&
        prev.paneLeft === l.paneLeft &&
        prev.box.left === l.box.left &&
        prev.box.top === l.box.top &&
        prev.box.right === l.box.right &&
        prev.box.bottom === l.box.bottom)
    )
      return;
    this.lastLayout = l;
    for (const f of this.layoutListeners) f(l);
  }

  // ---- geometry helpers -----------------------------------------------------------------------------

  /** pointer -> pane px (+ whether it is inside the main pane) */
  private local(e: { clientX: number; clientY: number }): XY & { inside: boolean } {
    const r = this.h.chart.chartElement().getBoundingClientRect();
    const x = e.clientX - r.left - this.prim.coords.paneLeft();
    const y = e.clientY - r.top;
    const v = this.prim.coords.view();
    return { x, y, inside: x >= 0 && x <= v.width && y >= 0 && y <= v.height };
  }

  private clampToPane(p: XY): XY {
    const v = this.prim.coords.view();
    return { x: Math.max(0, Math.min(v.width, p.x)), y: Math.max(0, Math.min(v.height, p.y)) };
  }

  private fromChart(e: Event): boolean {
    const t = e.target;
    return t instanceof Node && this.h.chart.chartElement().contains(t);
  }

  // ---- pointer --------------------------------------------------------------------------------------

  private claim(e: PointerEvent) {
    e.preventDefault();
    this.claimedTouch = e.pointerType !== "mouse";
  }

  private onTouchStart = (e: TouchEvent) => {
    if (!this.claimedTouch) return;
    // the pointerdown of this touch started a drawing gesture: keep it from the chart and the page scroll
    e.preventDefault();
    e.stopPropagation();
  };

  private onDown = (e: PointerEvent) => {
    if (this.gesture) return;
    if (!this.fromChart(e)) {
      // a press on another control (order ticket, tabs, …) ends the selection, so Delete / Ctrl+Z typed
      // there can never edit the chart
      if (!this.keepsSelection(e.target)) this.dropSelection();
      return;
    }
    if (e.defaultPrevented || inChartOverlay(e.target)) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const p = this.local(e);
    if (!p.inside) return;
    // a press on the chart ends inline text editing (commit on blur), even when the press is claimed below
    if (useDrawingUi.getState().editing && document.activeElement instanceof HTMLElement) document.activeElement.blur();
    const s = useTerminal.getState();
    const tool = parseTool(s.activeTool);
    if (tool) {
      if (this.beginCreate(tool, p, e)) this.claim(e);
      return;
    }
    const hit = this.prim.hit(p, e.pointerType === "mouse" ? TOL_MOUSE : TOL_TOUCH);
    if (!hit) {
      if (s.selectedDrawingId) s.selectDrawing(null);
      return; // pan / zoom as usual
    }
    if (hit.fill && s.selectedDrawingId !== hit.id) {
      // inside an unselected box: the chart keeps the gesture (pan); a plain click selects on release
      if (s.selectedDrawingId) s.selectDrawing(null);
      this.clickSelect = { id: hit.id, pointerId: e.pointerId, at: { clientX: e.clientX, clientY: e.clientY }, touch: e.pointerType !== "mouse" };
      return;
    }
    if (s.selectedDrawingId !== hit.id) s.selectDrawing(hit.id);
    const d = s.drawings.find((x) => x.id === hit.id);
    if (!d || d.locked || s.drawingsLocked) return; // selectable, not movable: the chart keeps the gesture
    const scale = d.points[0].scale;
    const anchor = this.prim.coords.toPoint(p.x, p.y, scale);
    if (!anchor) return;
    let offset = { x: 0, y: 0 };
    if (hit.part !== "body") {
      const sh = this.prim.shapes.find((z) => z.d.id === d.id);
      const idx = hit.part === "p0" || hit.part === "c0" ? 0 : hit.part === "p1" || hit.part === "c2" ? 1 : -1;
      if (sh) {
        const corner =
          idx >= 0
            ? sh.pts[idx]
            : hit.part === "c1"
              ? { x: sh.pts[1].x, y: sh.pts[0].y }
              : { x: sh.pts[0].x, y: sh.pts[1].y };
        offset = { x: p.x - corner.x, y: p.y - corner.y };
      }
    }
    this.gesture = {
      k: "drag",
      pointerId: e.pointerId,
      part: hit.part,
      start: d,
      anchor: { time: anchor.time, price: anchor.price },
      offset,
      startXY: p,
      moved: false,
      isNew: false,
    };
    this.claim(e);
    this.redraw();
  };

  /** returns true when the press was used */
  private beginCreate(tool: ToolSpec, p: XY, e: PointerEvent): boolean {
    // the server list has not loaded yet: nothing can be created against it
    if (!drawingsReady()) return false;
    const pt = this.prim.coords.toPoint(p.x, p.y, "btc");
    if (!pt) return false;
    const s = useTerminal.getState();
    if (this.pending) {
      // second click of a click … click drawing
      const first = this.prim.coords.toXY(this.pending.points[0]);
      if (first && Math.hypot(first.x - p.x, first.y - p.y) < MOVE_SLOP_MOUSE) return true; // same spot: keep waiting
      const done = this.withSecond(this.pending, p, e.shiftKey);
      this.pending = null;
      this.override = null;
      this.finishCreate(done);
      return true;
    }
    const draft = newDrawing(tool.kind, pointCount(tool.kind) === 1 ? [pt] : [pt, pt], { sticker: tool.sticker });
    if (s.selectedDrawingId) s.selectDrawing(null);
    if (pointCount(tool.kind) === 1) {
      this.gesture = {
        k: "drag",
        pointerId: e.pointerId,
        part: "body",
        start: draft,
        anchor: { time: pt.time, price: pt.price },
        offset: { x: 0, y: 0 },
        startXY: p,
        moved: false,
        isNew: true,
      };
    } else {
      this.gesture = { k: "create", pointerId: e.pointerId, start: p, moved: false, draft };
    }
    this.override = draft;
    this.redraw();
    return true;
  }

  /** a 2-point draft with its second point at pane position `p` */
  private withSecond(d: Drawing, p: XY, level: boolean): Drawing {
    const c = this.clampToPane(p);
    const q = this.prim.coords.toPoint(c.x, c.y, d.points[0].scale);
    if (!q) return d;
    return moveHandle(d, "p1", q.time, q.price, { level });
  }

  private onMove = (e: PointerEvent) => {
    const cs = this.clickSelect;
    if (cs && e.pointerId === cs.pointerId && Math.hypot(e.clientX - cs.at.clientX, e.clientY - cs.at.clientY) > (cs.touch ? MOVE_SLOP_TOUCH : MOVE_SLOP_MOUSE)) {
      this.clickSelect = null; // a pan, not a click
    }
    const g = this.gesture;
    if (g) {
      if (e.pointerId !== g.pointerId) return;
      const p = this.local(e);
      const start = g.k === "create" ? g.start : g.startXY;
      if (!g.moved && Math.hypot(p.x - start.x, p.y - start.y) > (e.pointerType === "mouse" ? MOVE_SLOP_MOUSE : MOVE_SLOP_TOUCH)) g.moved = true;
      if (!g.moved) return;
      if (g.k === "create") this.override = this.withSecond(g.draft, p, e.shiftKey);
      else this.override = this.dragged(g, p, e.shiftKey);
      this.redraw();
      return;
    }
    if (this.pending) {
      if (!this.fromChart(e) && !this.h.container.contains(e.target as Node)) return;
      this.override = this.withSecond(this.pending, this.local(e), e.shiftKey);
      this.redraw();
      return;
    }
    if (e.pointerType !== "mouse" || e.buttons !== 0 || !this.fromChart(e)) return;
    const p = this.local(e);
    const hit = p.inside && !parseTool(useTerminal.getState().activeTool) ? this.prim.hit(p, TOL_MOUSE) : null;
    const id = hit?.id ?? null;
    if (id !== this.hoverId) {
      this.hoverId = id;
      this.redraw();
    }
  };

  private dragged(g: Extract<Gesture, { k: "drag" }>, p: XY, shift: boolean): Drawing {
    const scale = g.start.points[0].scale;
    const coords = this.prim.coords;
    if (g.part === "body") {
      // the pointer may wander over other panels (window-level pointermove): keep the drag on the pane
      const c = this.clampToPane(p);
      const q = coords.toPoint(c.x, c.y, scale);
      if (!q) return this.override ?? g.start;
      const moved = moveBody(g.start, q.time - g.anchor.time, q.price - g.anchor.price);
      // refused (out of range): stay at the last valid position
      return moved === g.start ? (this.override ?? g.start) : moved;
    }
    const c = this.clampToPane({ x: p.x - g.offset.x, y: p.y - g.offset.y });
    const q = coords.toPoint(c.x, c.y, scale);
    if (!q) return this.override ?? g.start;
    return moveHandle(g.start, g.part, q.time, q.price, { level: shift });
  }

  private onUp = (e: PointerEvent) => {
    const cs = this.clickSelect;
    if (cs && e.pointerId === cs.pointerId) {
      this.clickSelect = null;
      const s = useTerminal.getState();
      if (!parseTool(s.activeTool) && s.drawings.some((d) => d.id === cs.id)) s.selectDrawing(cs.id);
    }
    const g = this.gesture;
    if (!g || e.pointerId !== g.pointerId) return;
    this.gesture = null;
    this.claimedTouch = false;
    const preview = this.override;
    this.override = null;
    if (g.k === "create") {
      if (g.moved && preview) this.finishCreate(preview);
      else {
        // a click: wait for the second click, previewing the drawing under the pointer meanwhile
        this.pending = g.draft;
        this.override = g.draft;
      }
      this.redraw();
      return;
    }
    const next = g.moved && preview ? preview : g.start;
    if (g.isNew) this.finishCreate(next);
    else if (!sameInput(next, g.start)) updateDrawing(g.start.id, () => next);
    this.redraw();
  };

  private finishCreate(d: Drawing) {
    const s = useTerminal.getState();
    s.setActiveTool("cursor");
    if (d.kind === "text") {
      // the note is added once its text is committed in the inline editor
      s.selectDrawing(d.id);
      useDrawingUi.getState().setEditing({ id: d.id, draft: d });
    } else {
      addDrawing(d, true);
    }
    this.redraw();
  }

  private onCancel = (e: PointerEvent) => {
    if (this.clickSelect?.pointerId === e.pointerId) this.clickSelect = null;
    if (this.gesture && e.pointerId === this.gesture.pointerId) this.cancelGesture();
  };

  /** the window lost focus mid-gesture (alt-tab): drop the gesture, the pointerup will never arrive */
  private onBlur = () => {
    if (this.gesture) this.cancelGesture();
  };

  cancelGesture() {
    this.clickSelect = null;
    this.gesture = null;
    this.pending = null;
    this.override = null;
    this.claimedTouch = false;
    this.redraw();
  }

  private onLeave = () => {
    if (this.hoverId) {
      this.hoverId = null;
      this.redraw();
    }
  };

  private onDblClick = (e: MouseEvent) => {
    if (!this.fromChart(e) || parseTool(useTerminal.getState().activeTool)) return;
    const p = this.local(e);
    if (!p.inside) return;
    const hit = this.prim.hit(p, TOL_MOUSE);
    if (!hit) return;
    const s = useTerminal.getState();
    const d = s.drawings.find((x) => x.id === hit.id);
    if (!d || (d.kind !== "text" && d.kind !== "hline")) return;
    e.preventDefault();
    e.stopPropagation();
    if (d.locked || s.drawingsLocked) return;
    this.edit(d.id);
  };

  /** open the inline editor for a text note / horizontal-line label */
  edit(id: string) {
    const s = useTerminal.getState();
    const d = s.drawings.find((x) => x.id === id);
    if (!d || (d.kind !== "text" && d.kind !== "hline")) return;
    s.selectDrawing(id);
    useDrawingUi.getState().setEditing({ id, draft: null });
  }

  /** the same drawing re-anchored to the other price scale, keeping its on-screen position */
  convertScale(d: Drawing, scale: DrawingScale): Drawing | null {
    const coords = this.prim.coords;
    const points: DrawingPoint[] = [];
    for (const p of d.points) {
      const y = coords.priceToY(p.price, p.scale);
      const price = y == null ? null : coords.yToPrice(y, scale);
      if (price == null) return null;
      points.push(makePoint(p.time, price, scale));
    }
    return { ...d, points };
  }

  // ---- keyboard -------------------------------------------------------------------------------------

  /** pressing / focusing this element keeps the selection (the chart's own overlays and the tools rail) */
  private keepsSelection(t: EventTarget | null): boolean {
    if (!(t instanceof Node)) return true;
    if (this.h.container.contains(t)) return true;
    const el = t instanceof Element ? t : t.parentElement;
    return !!el?.closest(KEEP_SELECTION);
  }

  /** focus is somewhere chart shortcuts may act: the chart, the page body or the drawing UI */
  private keysForChart(t: EventTarget | null): boolean {
    if (typeof document === "undefined") return true;
    if (t == null || t === window || t === document || t === document.body || t === document.documentElement) return true;
    return t instanceof Node && this.keepsSelection(t);
  }

  private dropSelection() {
    const s = useTerminal.getState();
    if (s.selectedDrawingId && !this.gesture && !useDrawingUi.getState().editing) s.selectDrawing(null);
  }

  private onFocusIn = (e: FocusEvent) => {
    if (!this.keepsSelection(e.target)) this.dropSelection();
  };

  private onKey = (e: KeyboardEvent) => {
    if (e.defaultPrevented || isEditableTarget(e.target) || inChartOverlay(e.target)) return;
    if (useDrawingUi.getState().confirmClear) return;
    if (typeof document !== "undefined" && document.querySelector('[role="dialog"][aria-modal="true"]')) return;
    const s = useTerminal.getState();
    const mod = e.metaKey || e.ctrlKey;
    const key = e.key.toLowerCase();
    if (e.key === "Escape") {
      if (this.gesture || this.pending) this.cancelGesture();
      else if (parseTool(s.activeTool)) s.setActiveTool("cursor");
      else if (s.selectedDrawingId) s.selectDrawing(null);
      else return;
      e.preventDefault();
      return;
    }
    // destructive shortcuts only while focus is on the chart side, never on order / tab controls
    if (!this.keysForChart(e.target)) return;
    if ((e.key === "Delete" || e.key === "Backspace") && !mod) {
      if (!s.selectedDrawingId || this.gesture) return;
      e.preventDefault();
      deleteSelected();
    } else if (mod && !e.altKey && key === "z") {
      const redo = e.shiftKey;
      if (!(redo ? canRedoDrawings() : canUndoDrawings())) return; // nothing to do: leave the key alone
      e.preventDefault();
      this.cancelGesture();
      if (redo) redoDrawings();
      else undoDrawings();
    } else if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && key === "y") {
      if (!canRedoDrawings()) return;
      e.preventDefault();
      this.cancelGesture();
      redoDrawings();
    }
  };
}
