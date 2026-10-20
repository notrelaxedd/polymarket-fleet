// Price to beat: amber dashed line across the current window + amber tag on the right axis + a small
// "Price to beat" label; the previous window's PTB as a 45%-opacity line with its value.
// Attached to the right-scale (BTC) anchor series. Also widens BTC autoscale to keep the current PTB visible.
// The axis tag is pinned (fixedCoordinate) at the exact PTB price so it never gets stacked away from its line.

import { useTerminal } from "@/lib/store";
import { fmtBtc } from "../fmt";
import { C, monoFont } from "../theme";
import { previousWindow, windowList } from "../windows";
import type { ChartHandle } from "../context";
import { AxisTag, BasePrimitive, FnPaneView, FnRenderer, measureRuns, pill, type MediaScope, type TextRun } from "./base";

interface LineGeom {
  x1: number;
  x2: number;
  y: number;
  value: number;
}

export class PriceToBeatPrimitive extends BasePrimitive {
  private cur: LineGeom | null = null;
  private prev: LineGeom | null = null;
  private ptb: number | null = null;
  private readonly tag = new AxisTag();
  private readonly tags = [this.tag];
  private readonly views = [
    new FnPaneView(new FnRenderer((s) => this.drawLines(s)), "normal"),
    new FnPaneView(new FnRenderer((s) => this.drawLabels(s)), "top"),
  ];

  constructor(h: ChartHandle) {
    super(h);
    this.tag.bgColor = C.ptb;
    this.tag.fg = "#1A1406";
    this.tag.fixed = true;
  }

  /** y of the PTB axis tag when shown (other right-axis tags dodge it) */
  tagY(): number | null {
    return this.tag.show ? this.tag.y : null;
  }

  paneViews() {
    return this.views;
  }
  priceAxisViews() {
    return this.tags;
  }

  autoscaleInfo() {
    if (!this.enabled || this.ptb == null) return null;
    return { priceRange: { minValue: this.ptb, maxValue: this.ptb } };
  }

  updateAllViews() {
    this.cur = null;
    this.prev = null;
    this.tag.show = false;
    const st = useTerminal.getState();
    this.ptb = st.current?.priceToBeat ?? null;
    const series = this.series;
    if (!this.enabled || !series) return;
    const width = this.chart?.timeScale().width() ?? 0;
    const geom = (w: { startTs: number; endTs: number; priceToBeat: number | null } | null): LineGeom | null => {
      if (!w || w.priceToBeat == null) return null;
      const x1 = this.h.timeToX(w.startTs);
      const x2 = this.h.timeToX(w.endTs);
      const y = series.priceToCoordinate(w.priceToBeat);
      if (x1 == null || x2 == null || y == null || x2 < 0 || x1 > width) return null;
      return { x1, x2, y, value: w.priceToBeat };
    };
    this.cur = geom(st.current);
    this.prev = geom(previousWindow(windowList(st), st.current));
    if (st.current?.priceToBeat != null) {
      const y = series.priceToCoordinate(st.current.priceToBeat);
      if (y != null) {
        this.tag.y = y;
        this.tag.label = fmtBtc(st.current.priceToBeat);
        this.tag.show = true;
      }
    }
  }

  private drawLines({ ctx, width }: MediaScope) {
    const line = (g: LineGeom, color: string, w: number, dash: number[]) => {
      const y = Math.round(g.y) + 0.5;
      ctx.strokeStyle = color;
      ctx.lineWidth = w;
      ctx.setLineDash(dash);
      ctx.beginPath();
      ctx.moveTo(Math.max(g.x1, 0), y);
      ctx.lineTo(Math.min(g.x2, width), y);
      ctx.stroke();
    };
    if (this.prev) line(this.prev, C.ptbFaded, 1, [5, 4]);
    if (this.cur) line(this.cur, C.ptb, 1.25, [6, 4]);
    ctx.setLineDash([]);
  }

  private drawLabels({ ctx, width }: MediaScope) {
    ctx.font = monoFont(10, 600);
    if (this.prev) {
      // opaque pill right-aligned against the previous window's end (away from the new window's first
      // candles). Narrow windows (1m/5m) get the bare value, which may extend left past the window start.
      const p = this.prev;
      const right = Math.min(p.x2, width) - 6;
      const value = fmtBtc(p.value);
      const full: TextRun[] = [
        { text: "PTB ", color: C.label },
        { text: value, color: C.ptbMuted },
      ];
      const bare: TextRun[] = [{ text: value, color: C.ptbMuted }];
      const fits = (r: TextRun[], left: number) => measureRuns(ctx, r) + 10 <= right - left;
      const runs = fits(full, Math.max(p.x1, 0) + 4) ? full : fits(bare, 2) ? bare : null;
      if (runs) {
        pill(ctx, runs, right, Math.round(p.y) - 10, {
          bg: C.surface,
          border: "rgba(245, 185, 66, 0.28)",
          align: "right",
          h: 16,
          padX: 5,
        });
      }
    }
    if (this.cur) {
      const c = this.cur;
      const x1 = Math.max(c.x1, 0);
      if (Math.min(c.x2, width) - x1 > 110) {
        pill(ctx, [{ text: "Price to beat", color: C.ptb }], x1 + 6, Math.round(c.y) - 10, {
          bg: "rgba(42, 33, 17, 0.95)",
          align: "left",
          h: 16,
          padX: 5,
        });
      }
    }
  }
}
