// Canvas side of the position lines, attached to the odds (left-scale) anchor series:
//  - entry: blue dashed line across the pane + an up-triangle at every buy fill (time, price);
//  - TP green / SL red solid lines; ticket preview legs faint + dotted;
//  - a price tag per line on the left axis ("D 47¢" for a Down line: the scale is read against Up odds);
// and it places the HTML labels (LabelOverlay) in the same frame, so labels and lines never drift apart.
// The lines themselves come from `getLines()` (PositionLinesController builds them from the store).

import type { ISeriesPrimitiveAxisView } from "lightweight-charts";
import { C } from "../theme";
import type { ChartHandle } from "../context";
import { AxisTag, BasePrimitive, FnPaneView, FnRenderer, type MediaScope } from "../primitives/base";
import { LABEL_H, type LabelOverlay } from "./LabelOverlay";
import { axisTagText, layoutLabels, type LabelBox, type PositionLine } from "./model";

interface Geom {
  line: PositionLine;
  y: number;
  markers: { x: number; y: number }[];
}

const COLOR = { entry: C.odds, tp: C.up, sl: C.down } as const;
const DRAFT_COLOR = {
  entry: "rgba(91, 156, 255, 0.5)",
  tp: "rgba(52, 211, 153, 0.55)",
  sl: "rgba(248, 113, 113, 0.55)",
} as const;
const TAG = {
  entry: { bg: "#13213A", fg: C.oddsText },
  tp: { bg: C.up, fg: C.bg },
  sl: { bg: C.down, fg: C.bg },
} as const;
const DRAFT_TAG = { entry: TAG.entry, tp: { bg: "#123528", fg: C.up }, sl: { bg: "#3A1818", fg: C.down } } as const;

/** labels stay this far from the pane's right edge (BTC axis side) */
const LABEL_RIGHT_PAD = 12;

export class PositionLinesPrimitive extends BasePrimitive {
  getLines: () => readonly PositionLine[] = () => [];
  overlay: LabelOverlay | null = null;
  /** key of the line being dragged (drawn emphasised, its label placed first) */
  activeKey: string | null = null;
  hoverKey: string | null = null;

  private geoms: Geom[] = [];
  private tags: AxisTag[] = [];
  private visibleTags: AxisTag[] = [];
  private readonly views = [new FnPaneView(new FnRenderer((s) => this.draw(s)), "normal")];

  paneViews() {
    return this.views;
  }

  priceAxisViews(): readonly ISeriesPrimitiveAxisView[] {
    return this.visibleTags;
  }

  detached() {
    super.detached();
    this.overlay?.prune(new Set());
  }

  updateAllViews() {
    this.geoms = [];
    const series = this.series;
    const chart = this.h.chart;
    const lines = this.enabled && series ? this.getLines() : [];
    const paneW = series ? chart.timeScale().width() : 0;
    const paneH = series ? chart.panes()[0]?.getHeight() ?? 0 : 0;

    for (const line of lines) {
      const y = series?.priceToCoordinate(line.cents);
      if (y == null || y < -1 || y > paneH + 1) continue;
      const markers: Geom["markers"] = [];
      for (const m of line.markers) {
        const x = this.h.timeToX(m.ts);
        const my = series?.priceToCoordinate(m.cents);
        if (x == null || my == null || x < -8 || x > paneW + 8) continue;
        markers.push({ x, y: my });
      }
      this.geoms.push({ line, y, markers });
    }

    this.updateTags();
    this.placeLabels(paneW, paneH);
  }

  private updateTags() {
    while (this.tags.length < this.geoms.length) {
      const t = new AxisTag();
      t.fixed = false;
      this.tags.push(t);
    }
    this.geoms.forEach((g, i) => {
      const t = this.tags[i];
      const k = g.line.kind;
      const c = g.line.draft ? DRAFT_TAG[k] : TAG[k];
      t.y = g.y;
      t.label = axisTagText(g.line);
      t.bgColor = c.bg;
      t.fg = c.fg;
      t.show = true;
      t.tick = !g.line.draft;
    });
    for (let i = this.geoms.length; i < this.tags.length; i++) this.tags[i].show = false;
    this.visibleTags = this.tags.slice(0, this.geoms.length);
  }

  private placeLabels(paneW: number, paneH: number) {
    const ov = this.overlay;
    if (!ov) return;
    const chart = this.h.chart;
    const left = paneW > 0 ? chart.priceScale("left").width() : 0;
    ov.setFrame({ left, top: 0, width: Math.max(0, paneW), height: Math.max(0, paneH) });
    const keep = new Set<string>();
    const boxes: (LabelBox & { rank: number })[] = [];
    for (const g of this.geoms) {
      const key = g.line.key;
      keep.add(key);
      const w = ov.prepare(g.line, g.y, { active: key === this.activeKey, hover: key === this.hoverKey });
      // the dragged label keeps the rightmost slot, then TP/SL, then entries (drafts last)
      const rank = key === this.activeKey ? 0 : (g.line.kind === "entry" ? 2 : 1) + (g.line.draft ? 2 : 0);
      boxes.push({ key, y: g.y, w, rank });
    }
    ov.prune(keep);
    if (paneW <= 0) {
      for (const b of boxes) ov.place(b.key, null);
      return;
    }
    boxes.sort((a, b) => a.rank - b.rank);
    const placed = layoutLabels(boxes, paneW - LABEL_RIGHT_PAD, LABEL_H);
    for (const p of placed) {
      const top = Math.min(Math.max(p.top, 0), paneH - LABEL_H);
      ov.place(p.key, { left: p.left, top });
    }
  }

  private draw({ ctx, width }: MediaScope) {
    // TP/SL over entries; the hovered / dragged line last
    const order = [...this.geoms].sort((a, b) => weight(this, a) - weight(this, b));
    for (const g of order) {
      const k = g.line.kind;
      const hot = g.line.key === this.activeKey || g.line.key === this.hoverKey;
      const y = Math.round(g.y) + 0.5;
      ctx.strokeStyle = g.line.draft ? DRAFT_COLOR[k] : COLOR[k];
      ctx.lineWidth = hot ? 2 : k === "entry" ? 1.5 : 1.25;
      ctx.setLineDash(g.line.draft ? [2, 3] : k === "entry" ? [6, 4] : []);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // entry triangles: apex at the fill price, body below (buy marker)
    for (const g of order) {
      for (const m of g.markers) {
        const x = Math.round(m.x);
        const top = Math.round(m.y) + 1;
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x + 6, top + 9);
        ctx.lineTo(x - 6, top + 9);
        ctx.closePath();
        ctx.fillStyle = C.odds;
        ctx.fill();
        ctx.strokeStyle = C.bg;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }
  }
}

function weight(p: PositionLinesPrimitive, g: Geom): number {
  if (g.line.key === p.activeKey) return 4;
  if (g.line.key === p.hoverKey) return 3;
  return g.line.kind === "entry" ? 0 : g.line.draft ? 1 : 2;
}
