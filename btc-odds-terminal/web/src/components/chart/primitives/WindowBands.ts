// Window bands: previous window slightly darker, current window slightly lighter, strong vertical lines at
// every 15m boundary, and a label at the bottom of each band ("10:15–10:30 · SETTLED UP", "… · LIVE").
// Narrow bands (1m/5m timeframes, phones) fall back to shorter labels: status only, then the time range.
// Drawn in the background layer so grid, series and other primitives sit on top.

import type { MarketWindow } from "@shared/protocol";
import { useTerminal } from "@/lib/store";
import { C, monoFont } from "../theme";
import { rangeLabel } from "../time";
import { previousWindow, windowList } from "../windows";
import { BasePrimitive, FnPaneView, FnRenderer, crisp, measureRuns, pill, type MediaScope, type TextRun } from "./base";

interface BandGeom {
  x1: number;
  x2: number;
  fill: string | null;
  /** label candidates, longest first */
  labels: TextRun[][];
}

/** Rect reserved at the bottom of the pane by other primitives (e.g. the NOW label) — labels avoid it. */
export interface ReservedRect {
  left: number;
  right: number;
}

export class WindowBandsPrimitive extends BasePrimitive {
  private bands: BandGeom[] = [];
  private boundaries: number[] = [];
  /** bottom-row rect owned by another label (the NOW pill); band labels dodge it */
  getReserved: () => ReservedRect | null = () => null;

  private readonly view = new FnPaneView(
    new FnRenderer(null, (s) => this.drawBg(s)),
    "bottom",
  );
  private readonly labelView = new FnPaneView(new FnRenderer((s) => this.drawLabels(s)), "top");
  private readonly views = [this.view, this.labelView];

  paneViews() {
    return this.views;
  }

  updateAllViews() {
    this.bands = [];
    this.boundaries = [];
    if (!this.enabled || !this.chart) return;
    const st = useTerminal.getState();
    const list = windowList(st);
    const cur = st.current;
    const prev = previousWindow(list, cur);
    const width = this.chart.timeScale().width();
    const seen = new Set<number>();
    for (const w of list) {
      if (w.status === "upcoming" && (!cur || w.id !== cur.id)) continue;
      const x1 = this.h.timeToX(w.startTs);
      const x2 = this.h.timeToX(w.endTs);
      if (x1 == null || x2 == null || x2 < 0 || x1 > width) continue;
      const isCur = !!cur && w.id === cur.id;
      const isPrev = !!prev && w.id === prev.id;
      this.bands.push({
        x1,
        x2,
        fill: isCur ? C.bandCurrent : isPrev ? C.bandPrevious : null,
        labels: labelVariants(w, isCur, st.replay.active),
      });
      for (const x of [x1, x2]) {
        const k = Math.round(x);
        if (!seen.has(k) && x >= 0 && x <= width) {
          seen.add(k);
          this.boundaries.push(x);
        }
      }
    }
  }

  private drawBg({ ctx, height }: MediaScope) {
    for (const b of this.bands) {
      if (!b.fill) continue;
      ctx.fillStyle = b.fill;
      ctx.fillRect(b.x1, 0, b.x2 - b.x1, height);
    }
    ctx.strokeStyle = C.boundary;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const x of this.boundaries) {
      const cx = crisp(x);
      ctx.moveTo(cx, 0);
      ctx.lineTo(cx, height);
    }
    ctx.stroke();
  }

  private drawLabels({ ctx, width, height }: MediaScope) {
    ctx.font = monoFont(10, 500);
    const y = height - 14;
    const rowH = 22;
    for (const b of this.bands) {
      // the label stays inside the visible part of the band (a partly scrolled-off window keeps its label)
      const visL = Math.max(b.x1, 0);
      const visR = Math.min(b.x2, width);
      const runs = fitLabel(b.labels, visR - visL - 8, (r) => measureRuns(ctx, r) + 14);
      if (!runs) continue;
      const tw = measureRuns(ctx, runs) + 14;
      let cx = (visL + visR) / 2;
      let cy = y;
      const r = this.getReserved();
      if (r && cx + tw / 2 > r.left - 6 && cx - tw / 2 < r.right + 6) {
        // try to slide left/right within the band, otherwise lift it one row up
        const leftFit = r.left - 8 - tw / 2;
        const rightFit = r.right + 8 + tw / 2;
        if (leftFit - tw / 2 >= visL + 4) cx = leftFit;
        else if (rightFit + tw / 2 <= visR - 4) cx = rightFit;
        else cy = y - rowH;
      }
      pill(ctx, runs, cx, cy, { bg: "rgba(14, 18, 24, 0.88)", border: C.border, align: "center", h: 18, padX: 7 });
    }
  }
}

/** First label candidate whose measured width fits `avail`, else null. */
export function fitLabel<T>(candidates: readonly T[], avail: number, measure: (c: T) => number): T | null {
  for (const c of candidates) if (measure(c) <= avail) return c;
  return null;
}

/** Band label candidates, longest first: "10:15–10:30 · SETTLED UP" -> "SETTLED UP" -> "10:15–10:30" -> "UP". */
export function labelVariants(w: MarketWindow, isCur: boolean, replay = false): TextRun[][] {
  const range: TextRun = { text: rangeLabel(w.startTs, w.endTs), color: C.muted };
  const sep: TextRun = { text: " · ", color: C.label };
  // during a replay the "current" window is the replayed one: never call it LIVE
  const status: TextRun | null = isCur && replay ? { text: "REPLAY", color: C.ptb } : statusRun(w, isCur);
  if (!status) return [[range]];
  const out: TextRun[][] = [[range, sep, status], [status], [range]];
  const short = status.text.replace(/^SETTLED /, "");
  if (short !== status.text) out.push([{ ...status, text: short }]);
  return out;
}

function statusRun(w: MarketWindow, isCur: boolean): TextRun | null {
  if (isCur) return { text: "LIVE", color: C.oddsText };
  if (w.result) {
    const up = w.result === "up";
    const settled = w.status === "settled" ? "SETTLED " : "";
    return { text: `${settled}${up ? "UP" : "DOWN"}`, color: up ? C.up : C.down };
  }
  if (w.status === "closed") return { text: "AWAITING SETTLE", color: C.label };
  if (w.status === "live") return { text: "LIVE", color: C.oddsText };
  return null;
}
