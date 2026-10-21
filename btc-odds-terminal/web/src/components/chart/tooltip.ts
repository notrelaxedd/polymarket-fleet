// Crosshair tooltip card (HTML, mono). Built imperatively and updated with textContent so a 60 Hz
// crosshair never goes through React.

import { C, MONO_FALLBACK } from "./theme";

export interface TooltipData {
  timeLabel: string;
  btc: string;
  dPtb: string | null;
  dPtbColor: string;
  upBA: string | null;
  fair: string | null;
  edge: string | null;
  edgeColor: string;
  tLeft: string | null;
}

type RowKey = "btc" | "dPtb" | "upBA" | "fair" | "edge" | "tLeft";

const ROWS: { key: RowKey; label: string; color: string }[] = [
  { key: "btc", label: "BTC", color: C.text },
  { key: "dPtb", label: "Δ PTB", color: C.text },
  { key: "upBA", label: "Up b/a", color: C.oddsText },
  { key: "fair", label: "Fair", color: C.fair },
  { key: "edge", label: "Edge", color: C.text },
  { key: "tLeft", label: "Time left", color: C.ptb },
];

export class Tooltip {
  readonly el: HTMLDivElement;
  private readonly head: HTMLDivElement;
  private readonly vals = {} as Record<RowKey, HTMLSpanElement>;
  private visible = false;

  constructor(parent: HTMLElement) {
    const el = document.createElement("div");
    el.setAttribute("role", "tooltip");
    Object.assign(el.style, {
      position: "absolute",
      left: "0px",
      top: "0px",
      zIndex: "5",
      pointerEvents: "none",
      display: "none",
      minWidth: "172px",
      padding: "8px 10px",
      background: C.surface2,
      border: `1px solid ${C.borderStrong}`,
      borderRadius: "8px",
      boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
      fontFamily: `var(--font-mono), ${MONO_FALLBACK}`,
      fontSize: "11px",
      lineHeight: "17px",
      fontVariantNumeric: "tabular-nums",
      color: C.text,
      willChange: "transform",
    } satisfies Partial<CSSStyleDeclaration>);
    this.head = document.createElement("div");
    Object.assign(this.head.style, {
      color: C.label,
      fontSize: "10px",
      letterSpacing: "0.04em",
      marginBottom: "4px",
      paddingBottom: "4px",
      borderBottom: `1px solid ${C.border}`,
    });
    el.appendChild(this.head);
    for (const r of ROWS) {
      const row = document.createElement("div");
      Object.assign(row.style, { display: "flex", justifyContent: "space-between", gap: "16px" });
      const l = document.createElement("span");
      l.textContent = r.label;
      l.style.color = C.label;
      const v = document.createElement("span");
      v.style.color = r.color;
      row.append(l, v);
      el.appendChild(row);
      this.vals[r.key] = v;
    }
    parent.appendChild(el);
    this.el = el;
  }

  show(d: TooltipData, x: number, y: number, boundsW: number, boundsH: number) {
    this.head.textContent = d.timeLabel;
    this.vals.btc.textContent = d.btc;
    this.vals.dPtb.textContent = d.dPtb ?? "—";
    this.vals.dPtb.style.color = d.dPtb ? d.dPtbColor : C.label;
    this.vals.upBA.textContent = d.upBA ?? "—";
    this.vals.fair.textContent = d.fair ?? "—";
    this.vals.edge.textContent = d.edge ?? "—";
    this.vals.edge.style.color = d.edge ? d.edgeColor : C.label;
    this.vals.tLeft.textContent = d.tLeft ?? "—";
    if (!this.visible) {
      this.el.style.display = "block";
      this.visible = true;
    }
    const w = this.el.offsetWidth;
    const h = this.el.offsetHeight;
    const gap = 16;
    let left = x + gap;
    if (left + w > boundsW - 4) left = x - gap - w;
    let top = y + gap;
    if (top + h > boundsH - 4) top = Math.max(4, y - gap - h);
    this.el.style.transform = `translate(${Math.round(Math.max(4, left))}px, ${Math.round(top)}px)`;
  }

  hide() {
    if (!this.visible) return;
    this.el.style.display = "none";
    this.visible = false;
  }

  destroy() {
    this.el.remove();
  }
}
