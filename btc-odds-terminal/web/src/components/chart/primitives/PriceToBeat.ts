// Price to beat: amber dashed line across the current window + amber tag on the right axis + a small
// "Price to beat" label; the previous window's PTB at 45% opacity with its value.
// Attached to the right-scale (BTC) anchor series. Also widens BTC autoscale to keep the current PTB visible.

import { useTerminal } from "@/lib/store";
import { fmtBtc } from "../fmt";
import { C, monoFont } from "../theme";
import { previousWindow, windowList } from "../windows";
import { AxisTag, BasePrimitive, FnPaneView, FnRenderer, pill, type MediaScope } from "./base";

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
  private readonly views = [new FnPaneView(new FnRenderer((s) => this.draw(s)), "normal")];

  constructor(...args: ConstructorParameters<typeof BasePrimitive>) {
    super(...args);
    this.tag.bgColor = C.ptb;
    this.tag.fg = "#1A1406";
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

  private draw({ ctx, width }: MediaScope) {
    ctx.font = monoFont(10, 600);
    if (this.prev) {
      const p = this.prev;
      ctx.strokeStyle = C.ptbFaded;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(Math.max(p.x1, 0), Math.round(p.y) + 0.5);
      ctx.lineTo(Math.min(p.x2, width), Math.round(p.y) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
      const lx = Math.max(p.x1, 0) + 6;
      if (Math.min(p.x2, width) - lx > 120) {
        ctx.globalAlpha = 0.45;
        ctx.textBaseline = "bottom";
        ctx.fillStyle = C.ptb;
        ctx.fillText(`PTB ${fmtBtc(p.value)}`, lx, Math.round(p.y) - 3);
        ctx.globalAlpha = 1;
      }
    }
    if (this.cur) {
      const c = this.cur;
      const y = Math.round(c.y) + 0.5;
      const x1 = Math.max(c.x1, 0);
      const x2 = Math.min(c.x2, width);
      ctx.strokeStyle = C.ptb;
      ctx.lineWidth = 1.25;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(x1, y);
      ctx.lineTo(x2, y);
      ctx.stroke();
      ctx.setLineDash([]);
      if (x2 - x1 > 110) {
        pill(ctx, [{ text: "Price to beat", color: C.ptb }], x1 + 6, y - 11, {
          bg: "rgba(42, 33, 17, 0.92)",
          align: "left",
          h: 16,
          padX: 5,
        });
      }
    }
  }
}
