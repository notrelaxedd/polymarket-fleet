// HTML labels of the position lines, positioned over the main pane. Plain DOM (not React) so the labels move
// in the same frame as the canvas lines (PositionLinesPrimitive.updateAllViews places them).
//
// Hit-testing: only TP/SL labels (and the entry label's "+TP" / "+SL" chips) take pointer events. The overlay
// root itself is pointer-events: none, so everywhere else the chart pans / zooms / shows its crosshair as
// usual. A label owns its gesture: pointerdown is stopped there and the pointer captured, so the chart never
// sees a drag that starts on a handle. Mouse, pen and touch share the pointer path (touch-action: none on the
// handle); on coarse pointers an invisible extender grows the hit area to >= 44px without changing the look.
// Wheel over a label is forwarded to the chart underneath so zooming is not interrupted.
// A gesture the browser cancels (pointercancel / lost capture: touch scroll takeover, system gesture, label
// detached) reverts the leg; only pointerup commits.
// Keyboard: a TP/SL label holds a focusable vertical slider (arrows ±1¢, PageUp/PageDown ±5¢, Delete removes,
// Esc leaves) and, next to it (not inside: a slider's children are presentational), a tabbable ✕ button.
// After a removal focus moves to the "+TP" / "+SL" chip that replaces the leg (or the other leg), and a polite
// live region announces it.

import { C, MONO_FALLBACK } from "../theme";
import type { LabelRun, PositionLine, RunTone } from "./model";

export interface OverlayCallbacks {
  /** a TP/SL handle was grabbed; return false to refuse the drag */
  dragStart(key: string): boolean;
  /** pane y (CSS px) the line should follow */
  dragMove(key: string, y: number): void;
  /** released (commit) or Esc / lost (revert) */
  dragEnd(key: string, commit: boolean): void;
  nudge(key: string, steps: number): void;
  remove(key: string): void;
  add(positionId: string, kind: "tp" | "sl"): void;
  hover(key: string | null): void;
}

export interface OverlayFrame {
  left: number;
  top: number;
  width: number;
  height: number;
}

const TONE: Record<RunTone, string> = {
  "side-up": C.up,
  "side-down": C.down,
  entry: C.oddsText,
  tp: C.up,
  sl: C.down,
  muted: C.label,
  pos: C.up,
  neg: C.down,
  flat: C.text2,
};

const ACCENT = { entry: C.odds, tp: C.up, sl: C.down } as const;
const ACCENT_SOFT = {
  entry: "rgba(91, 156, 255, 0.5)",
  tp: "rgba(52, 211, 153, 0.5)",
  sl: "rgba(248, 113, 113, 0.5)",
} as const;
const TINT = { entry: "#13213A", tp: "#123528", sl: "#3A1818" } as const;

export const LABEL_H = 20;
const FONT = `var(--font-mono), ${MONO_FALLBACK}`;

const SVG_NS = "http://www.w3.org/2000/svg";

function gripIcon(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "6");
  svg.setAttribute("height", "10");
  svg.setAttribute("viewBox", "0 0 6 10");
  svg.setAttribute("aria-hidden", "true");
  for (const [cx, cy] of [
    [1, 1],
    [5, 1],
    [1, 5],
    [5, 5],
    [1, 9],
    [5, 9],
  ]) {
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", String(cx));
    c.setAttribute("cy", String(cy));
    c.setAttribute("r", "1");
    c.setAttribute("fill", "currentColor");
    svg.appendChild(c);
  }
  return svg;
}

function closeIcon(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", "8");
  svg.setAttribute("height", "8");
  svg.setAttribute("viewBox", "0 0 8 8");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", "M1 1l6 6M7 1L1 7");
  p.setAttribute("stroke", "currentColor");
  p.setAttribute("stroke-width", "1.5");
  p.setAttribute("stroke-linecap", "round");
  svg.appendChild(p);
  return svg;
}

/** keyboard focus ring on a button inside a label */
function focusRing(b: HTMLElement) {
  b.addEventListener("focus", () => {
    if (b.matches(":focus-visible")) b.style.outline = `2px solid ${C.odds}`;
  });
  b.addEventListener("blur", () => {
    b.style.outline = "none";
  });
}

const coarse = () => typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)").matches;

/** invisible child that grows the hit area on touch screens */
function extender(el: HTMLElement, x: number, y: number) {
  const s = document.createElement("span");
  s.setAttribute("aria-hidden", "true");
  Object.assign(s.style, { position: "absolute", inset: `-${y}px -${x}px`, background: "transparent" });
  el.appendChild(s);
}

interface Node {
  el: HTMLDivElement;
  /** focus / ARIA target: the slider of a TP/SL label, the label itself for an entry */
  knob: HTMLElement;
  text: HTMLSpanElement;
  /** signature of the rendered content (re-render only on change) */
  sig: string;
  /** signature of the "+TP" / "+SL" chips (rebuilt only on change, so a focused chip keeps focus) */
  chipSig: string;
  width: number;
  line: PositionLine;
  /** current line y (pane px), for the grab offset */
  y: number;
  chips: HTMLDivElement | null;
}

interface Drag {
  key: string;
  pointerId: number;
  el: HTMLElement;
  /** pointer y minus line y at grab time */
  grabDy: number;
}

export class LabelOverlay {
  readonly root: HTMLDivElement;
  private readonly nodes = new Map<string, Node>();
  private drag: Drag | null = null;
  private hoverKey: string | null = null;
  private readonly touch = coarse();
  private frame: OverlayFrame = { left: 0, top: 0, width: 0, height: 0 };
  private readonly live: HTMLDivElement;
  /** a focused leg was removed: where focus goes once its label is gone */
  private refocus: { key: string; positionId: string | null; kind: "tp" | "sl"; until: number } | null = null;

  constructor(
    parent: HTMLElement,
    private readonly cb: OverlayCallbacks,
  ) {
    const root = document.createElement("div");
    root.dataset.chartOverlay = "position-lines";
    Object.assign(root.style, {
      position: "absolute",
      left: "0px",
      top: "0px",
      width: "0px",
      height: "0px",
      overflow: "hidden",
      pointerEvents: "none",
      zIndex: "4", // above the chart canvas, below the crosshair tooltip (5)
    } satisfies Partial<CSSStyleDeclaration>);
    const live = document.createElement("div");
    live.setAttribute("aria-live", "polite");
    live.setAttribute("role", "status");
    Object.assign(live.style, {
      position: "absolute",
      width: "1px",
      height: "1px",
      overflow: "hidden",
      clip: "rect(0 0 0 0)",
      whiteSpace: "nowrap",
    } satisfies Partial<CSSStyleDeclaration>);
    root.appendChild(live);
    this.live = live;
    parent.appendChild(root);
    this.root = root;
    window.addEventListener("keydown", this.onWindowKey, true);
    // widths are cached per content; the web font changes them once it has loaded
    document.fonts?.ready.then(() => {
      for (const n of this.nodes.values()) n.width = 0;
      this.onFontsReady?.();
    });
  }

  /** called once the web fonts are ready (labels need a re-layout) */
  onFontsReady: (() => void) | null = null;

  destroy() {
    this.endDrag(false);
    window.removeEventListener("keydown", this.onWindowKey, true);
    this.root.remove();
    this.nodes.clear();
  }

  dragging(): string | null {
    return this.drag?.key ?? null;
  }

  hovered(): string | null {
    return this.hoverKey;
  }

  setFrame(f: OverlayFrame) {
    const p = this.frame;
    if (p.left === f.left && p.top === f.top && p.width === f.width && p.height === f.height) return;
    this.frame = f;
    Object.assign(this.root.style, { left: `${f.left}px`, top: `${f.top}px`, width: `${f.width}px`, height: `${f.height}px` });
  }

  /** Create / refresh the label of `line` and return its width (CSS px). */
  prepare(line: PositionLine, y: number, state: { active: boolean; hover: boolean }): number {
    let n = this.nodes.get(line.key);
    if (!n) {
      n = this.create(line);
      this.nodes.set(line.key, n);
    }
    n.line = line;
    n.y = y;
    const sig = line.runs.map((r) => `${r.tone}:${r.text}`).join("|");
    if (sig !== n.sig) {
      n.sig = sig;
      this.fill(n, line.runs);
      n.width = 0;
    }
    const chipSig = `${line.addTp ? 1 : 0}${line.addSl ? 1 : 0}`;
    if (chipSig !== n.chipSig) {
      n.chipSig = chipSig;
      this.updateChips(n, line);
      n.width = 0;
    }
    this.styleState(n, state);
    if (line.kind !== "entry") {
      n.knob.setAttribute("aria-valuenow", String(line.cents));
      n.knob.setAttribute("aria-valuetext", line.aria);
      if (line.bounds) {
        n.knob.setAttribute("aria-valuemin", String(line.bounds.lo));
        n.knob.setAttribute("aria-valuemax", String(line.bounds.hi));
        n.knob.removeAttribute("aria-disabled");
        n.el.style.cursor = "ns-resize";
      } else {
        // no fresh quote (or no room): the leg cannot move right now
        n.knob.setAttribute("aria-disabled", "true");
        n.el.style.cursor = "not-allowed";
      }
    }
    n.knob.setAttribute("aria-label", line.aria);
    if (!n.width) {
      n.el.style.display = "flex";
      n.width = Math.ceil(n.el.offsetWidth);
    }
    return n.width;
  }

  /** Position a prepared label (null = hide it). */
  place(key: string, pos: { left: number; top: number } | null) {
    const n = this.nodes.get(key);
    if (!n) return;
    if (!pos) {
      n.el.style.display = "none";
      return;
    }
    n.el.style.display = "flex";
    n.el.style.transform = `translate(${pos.left}px, ${pos.top}px)`;
  }

  /** Remove labels whose line is gone (a drag on one of them ends, reverting). */
  prune(keep: ReadonlySet<string>) {
    for (const [key, n] of this.nodes) {
      if (keep.has(key)) continue;
      if (this.drag?.key === key) this.endDrag(false);
      if (this.hoverKey === key) this.setHover(null);
      n.el.remove();
      this.nodes.delete(key);
    }
    this.applyRefocus();
  }

  private applyRefocus() {
    const r = this.refocus;
    if (!r) return;
    if (this.nodes.has(r.key)) {
      // not removed (yet): the write may still be pending; give up after a while
      if (Date.now() > r.until) this.refocus = null;
      return;
    }
    this.refocus = null;
    const other = r.kind === "tp" ? "sl" : "tp";
    const prefix = r.positionId ?? "draft";
    const chip = r.positionId ? this.nodes.get(`${r.positionId}:entry`)?.chips?.querySelector<HTMLElement>(`[data-add="${r.kind}"]`) : null;
    const target = chip ?? this.nodes.get(`${prefix}:${other}`)?.knob ?? null;
    target?.focus({ preventScroll: true });
  }

  private removeLeg(key: string) {
    const n = this.nodes.get(key);
    if (!n || n.line.kind === "entry") return;
    const kind = n.line.kind;
    const what = kind === "tp" ? "Take profit" : "Stop loss";
    const line = n.line;
    // set before removing: the chart may re-render (and prune this label) synchronously
    if (n.el.contains(document.activeElement)) this.refocus = { key, positionId: line.positionId, kind, until: Date.now() + 3000 };
    this.cb.remove(key);
    this.live.textContent = line.draft ? `${what} turned off for the next order` : `${what} removed`;
  }

  // ---- DOM ------------------------------------------------------------------------------------------

  private create(line: PositionLine): Node {
    const el = document.createElement("div");
    const leg = line.kind !== "entry";
    Object.assign(el.style, {
      position: "absolute",
      left: "0px",
      top: "0px",
      height: `${LABEL_H}px`,
      display: "flex",
      alignItems: "center",
      gap: "6px",
      padding: leg ? "0 3px 0 6px" : "0 7px",
      boxSizing: "border-box",
      borderRadius: "4px",
      border: "1px solid transparent",
      fontFamily: FONT,
      fontSize: "11px",
      fontWeight: "500",
      lineHeight: `${LABEL_H - 2}px`,
      fontVariantNumeric: "tabular-nums",
      whiteSpace: "nowrap",
      userSelect: "none",
      webkitUserSelect: "none",
      willChange: "transform",
      pointerEvents: leg ? "auto" : "none",
      touchAction: leg ? "none" : "auto",
      cursor: leg ? "ns-resize" : "default",
      outline: "none",
    } satisfies Partial<CSSStyleDeclaration>);
    el.dataset.lineKey = line.key;
    el.dataset.kind = line.kind;
    if (line.draft) el.dataset.draft = "1";
    const text = document.createElement("span");
    text.style.display = "inline-flex";
    let knob: HTMLElement = el;
    if (leg) {
      if (this.touch) extender(el, 4, 12);
      // the slider (grip + text) and the ✕ are siblings: a slider's children are presentational
      knob = document.createElement("span");
      Object.assign(knob.style, { display: "inline-flex", alignItems: "center", gap: "6px", outline: "none" } satisfies Partial<
        CSSStyleDeclaration
      >);
      const grip = document.createElement("span");
      grip.appendChild(gripIcon());
      Object.assign(grip.style, { display: "inline-flex", color: C.muted, marginRight: "-1px" });
      knob.appendChild(grip);
      knob.appendChild(text);
      knob.tabIndex = 0;
      knob.setAttribute("role", "slider");
      knob.setAttribute("aria-orientation", "vertical");
      knob.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown PageUp PageDown Delete");
      el.appendChild(knob);
      el.appendChild(this.closeButton(line));
      el.title = `Drag to move the ${line.kind === "tp" ? "take profit" : "stop loss"} · ✕ or Delete removes it`;
      this.bindLeg(el, knob, line.key);
    } else {
      el.setAttribute("role", "note");
      el.appendChild(text);
    }
    this.root.appendChild(el);
    return { el, knob, text, sig: "", chipSig: "", width: 0, line, y: 0, chips: null };
  }

  private closeButton(line: PositionLine): HTMLButtonElement {
    const b = document.createElement("button");
    b.type = "button";
    const what = line.kind === "tp" ? "take profit" : "stop loss";
    b.setAttribute("aria-label", line.draft ? `Turn off the ${what} of the next order` : `Remove ${what}`);
    b.title = line.draft ? `Turn off ${what}` : `Remove ${what}`;
    Object.assign(b.style, {
      position: "relative",
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      width: "16px",
      height: "16px",
      padding: "0",
      margin: "0",
      border: "0",
      borderRadius: "3px",
      background: "transparent",
      color: C.muted,
      cursor: "pointer",
      touchAction: "manipulation",
    } satisfies Partial<CSSStyleDeclaration>);
    b.appendChild(closeIcon());
    if (this.touch) extender(b, 10, 14);
    b.addEventListener("pointerdown", (e) => e.stopPropagation());
    b.addEventListener("pointerenter", () => {
      b.style.background = C.borderStrong;
      b.style.color = C.text;
    });
    b.addEventListener("pointerleave", () => {
      b.style.background = "transparent";
      b.style.color = C.muted;
    });
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      const key = b.closest<HTMLElement>("[data-line-key]")?.dataset.lineKey;
      if (key) this.removeLeg(key);
    });
    b.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        b.blur();
      }
    });
    focusRing(b);
    return b;
  }

  private fill(n: Node, runs: readonly LabelRun[]) {
    n.text.replaceChildren(
      ...runs.map((r) => {
        const s = document.createElement("span");
        s.textContent = r.text;
        s.style.color = TONE[r.tone];
        s.style.whiteSpace = "pre";
        if (r.tone === "side-up" || r.tone === "side-down" || r.tone === "tp" || r.tone === "sl") s.style.fontWeight = "600";
        return s;
      }),
    );
  }

  private updateChips(n: Node, line: PositionLine) {
    if (line.kind !== "entry" || !line.positionId || (!line.addTp && !line.addSl)) {
      n.chips?.remove();
      n.chips = null;
      return;
    }
    const pid = line.positionId;
    if (!n.chips) {
      n.chips = document.createElement("div");
      Object.assign(n.chips.style, { display: "inline-flex", gap: "3px", marginRight: "-4px" });
      n.el.appendChild(n.chips);
    }
    const mk = (kind: "tp" | "sl") => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = kind === "tp" ? "+TP" : "+SL";
      b.dataset.add = kind;
      const what = kind === "tp" ? "take profit" : "stop loss";
      b.setAttribute("aria-label", `Add a ${what}`);
      b.title = `Add a ${what} line, then drag it`;
      Object.assign(b.style, {
        position: "relative",
        pointerEvents: "auto",
        height: "14px",
        padding: "0 4px",
        border: `1px solid ${ACCENT_SOFT[kind]}`,
        borderRadius: "3px",
        background: TINT[kind],
        color: ACCENT[kind],
        fontFamily: FONT,
        fontSize: "10px",
        fontWeight: "600",
        lineHeight: "12px",
        cursor: "pointer",
        touchAction: "manipulation",
      } satisfies Partial<CSSStyleDeclaration>);
      if (this.touch) extender(b, 4, 15);
      focusRing(b);
      b.addEventListener("pointerdown", (e) => e.stopPropagation());
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        this.cb.add(pid, kind);
      });
      return b;
    };
    n.chips.replaceChildren(...(line.addTp ? [mk("tp")] : []), ...(line.addSl ? [mk("sl")] : []));
  }

  private styleState(n: Node, s: { active: boolean; hover: boolean }) {
    const k = n.line.kind;
    const hot = s.active || s.hover;
    const st = n.el.style;
    st.background = n.line.draft ? "rgba(14, 18, 24, 0.9)" : "rgba(21, 27, 36, 0.96)";
    st.borderStyle = n.line.draft ? "dashed" : "solid";
    st.borderColor = hot ? ACCENT[k] : ACCENT_SOFT[k];
    st.boxShadow = s.active
      ? `0 0 0 2px ${ACCENT_SOFT[k]}, 0 6px 16px rgba(0,0,0,0.45)`
      : hot
        ? "0 4px 12px rgba(0,0,0,0.4)"
        : "0 2px 6px rgba(0,0,0,0.3)";
    st.zIndex = s.active ? "3" : hot ? "2" : k === "entry" ? "0" : "1";
  }

  // ---- interaction ----------------------------------------------------------------------------------

  private bindLeg(el: HTMLDivElement, knob: HTMLElement, key: string) {
    el.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if ((e.target as Element).closest("button")) return;
      e.preventDefault(); // no text selection / focus steal / emulated mouse events
      e.stopPropagation();
      if (this.drag) return;
      const n = this.nodes.get(key);
      if (!n || !this.cb.dragStart(key)) return;
      try {
        el.setPointerCapture(e.pointerId);
      } catch {
        /* pointer already gone */
      }
      const top = this.root.getBoundingClientRect().top;
      this.drag = { key, pointerId: e.pointerId, el, grabDy: e.clientY - top - n.y };
      knob.focus({ preventScroll: true });
    });
    el.addEventListener("pointermove", (e) => {
      const d = this.drag;
      if (!d || d.key !== key || e.pointerId !== d.pointerId) return;
      e.preventDefault();
      const top = this.root.getBoundingClientRect().top;
      this.cb.dragMove(key, e.clientY - top - d.grabDy);
    });
    // only a release commits; a gesture the browser takes away (or a lost capture) reverts
    const end = (commit: boolean) => (e: PointerEvent) => {
      const d = this.drag;
      if (!d || d.key !== key || e.pointerId !== d.pointerId) return;
      this.endDrag(commit);
    };
    el.addEventListener("pointerup", end(true));
    el.addEventListener("pointercancel", end(false));
    el.addEventListener("lostpointercapture", end(false));
    el.addEventListener("pointerenter", () => this.setHover(key));
    el.addEventListener("pointerleave", () => {
      if (this.hoverKey === key) this.setHover(null);
    });
    el.addEventListener("wheel", this.forwardWheel, { passive: false });
    knob.addEventListener("keydown", (e) => {
      if (this.drag) return;
      let steps = 0;
      if (e.key === "ArrowUp" || e.key === "ArrowRight") steps = e.shiftKey ? 5 : 1;
      else if (e.key === "ArrowDown" || e.key === "ArrowLeft") steps = e.shiftKey ? -5 : -1;
      else if (e.key === "PageUp") steps = 5;
      else if (e.key === "PageDown") steps = -5;
      else if (e.key === "Delete") {
        // Delete only: Backspace is too easy to hit for something that can drop a live stop loss
        e.preventDefault();
        e.stopPropagation(); // the drawings layer also listens for Del
        this.removeLeg(key);
        return;
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation(); // the drawings layer deselects on Esc
        knob.blur();
        return;
      }
      if (!steps) return;
      e.preventDefault();
      e.stopPropagation();
      this.cb.nudge(key, steps);
    });
    knob.addEventListener("focus", () => {
      if (knob.matches(":focus-visible")) el.style.outline = `2px solid ${C.odds}`;
      el.style.outlineOffset = "1px";
    });
    knob.addEventListener("blur", () => {
      el.style.outline = "none";
    });
  }

  private setHover(key: string | null) {
    if (this.hoverKey === key) return;
    this.hoverKey = key;
    this.cb.hover(key);
  }

  private endDrag(commit: boolean) {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    try {
      if (d.el.hasPointerCapture(d.pointerId)) d.el.releasePointerCapture(d.pointerId);
    } catch {
      /* element already detached */
    }
    this.cb.dragEnd(d.key, commit);
  }

  private onWindowKey = (e: KeyboardEvent) => {
    if (e.key !== "Escape" || !this.drag) return;
    e.preventDefault();
    e.stopPropagation();
    this.endDrag(false);
  };

  /** Re-dispatch a wheel over a label to the chart element below it. */
  private forwardWheel = (e: WheelEvent) => {
    const below = document.elementsFromPoint(e.clientX, e.clientY).find((el) => !this.root.contains(el));
    if (!below) return;
    e.preventDefault();
    below.dispatchEvent(
      new WheelEvent("wheel", {
        bubbles: true,
        cancelable: true,
        clientX: e.clientX,
        clientY: e.clientY,
        screenX: e.screenX,
        screenY: e.screenY,
        deltaX: e.deltaX,
        deltaY: e.deltaY,
        deltaZ: e.deltaZ,
        deltaMode: e.deltaMode,
        ctrlKey: e.ctrlKey,
        shiftKey: e.shiftKey,
        altKey: e.altKey,
        metaKey: e.metaKey,
      }),
    );
  };
}
