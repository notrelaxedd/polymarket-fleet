// Shared plumbing for series primitives. Each primitive in this folder:
//  - computes its geometry in `updateAllViews()` (called by the chart on every invalidation),
//  - draws in media (CSS px) coordinates through `FnRenderer`,
//  - can be toggled with `setEnabled()` and repainted with `update()`.

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitiveAxisView,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { ChartHandle } from "../context";

export type DrawTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];
export interface MediaScope {
  ctx: CanvasRenderingContext2D;
  width: number;
  height: number;
}

export class FnRenderer implements IPrimitivePaneRenderer {
  constructor(
    private readonly fg: ((s: MediaScope) => void) | null,
    private readonly bg: ((s: MediaScope) => void) | null = null,
  ) {}
  draw(target: DrawTarget) {
    if (this.fg) run(target, this.fg);
  }
  drawBackground(target: DrawTarget) {
    if (this.bg) run(target, this.bg);
  }
}

function run(target: DrawTarget, f: (s: MediaScope) => void) {
  target.useMediaCoordinateSpace(({ context, mediaSize }) => {
    context.save();
    try {
      f({ ctx: context, width: mediaSize.width, height: mediaSize.height });
    } finally {
      context.restore();
    }
  });
}

export class FnPaneView implements IPrimitivePaneView {
  constructor(
    private readonly r: IPrimitivePaneRenderer,
    private readonly z: PrimitivePaneViewZOrder = "normal",
  ) {}
  zOrder() {
    return this.z;
  }
  renderer() {
    return this.r;
  }
}

/** Mutable price-axis tag (label on the price scale). */
export class AxisTag implements ISeriesPrimitiveAxisView {
  y = 0;
  label = "";
  fg = "#0A0D12";
  bgColor = "#F5B942";
  show = false;
  coordinate() {
    return this.y;
  }
  text() {
    return this.label;
  }
  textColor() {
    return this.fg;
  }
  backColor() {
    return this.bgColor;
  }
  visible() {
    return this.show;
  }
  tickVisible() {
    return this.show;
  }
}

export abstract class BasePrimitive implements ISeriesPrimitive<Time> {
  protected chart: IChartApiBase<Time> | null = null;
  protected series: ISeriesApi<SeriesType, Time> | null = null;
  private requestUpdate: (() => void) | null = null;
  protected enabled = true;

  constructor(protected readonly h: ChartHandle) {}

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart;
    this.series = p.series;
    this.requestUpdate = p.requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  setEnabled(on: boolean) {
    if (on === this.enabled) return;
    this.enabled = on;
    this.update();
  }

  isEnabled() {
    return this.enabled;
  }

  /** request a recompute + repaint */
  update() {
    this.requestUpdate?.();
  }

  abstract updateAllViews(): void;
}

// ---- small canvas helpers ----------------------------------------------------------------------------

/** crisp 1px vertical/horizontal lines in media space */
export const crisp = (v: number) => Math.round(v) + 0.5;

export function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

export interface TextRun {
  text: string;
  color: string;
}

/** measure a sequence of differently-colored text runs (font must already be set) */
export function measureRuns(ctx: CanvasRenderingContext2D, runs: readonly TextRun[]): number {
  let w = 0;
  for (const r of runs) w += ctx.measureText(r.text).width;
  return w;
}

/** draw runs left-to-right starting at x (textBaseline should be "middle") */
export function drawRuns(ctx: CanvasRenderingContext2D, runs: readonly TextRun[], x: number, y: number) {
  let cx = x;
  for (const r of runs) {
    ctx.fillStyle = r.color;
    ctx.fillText(r.text, cx, y);
    cx += ctx.measureText(r.text).width;
  }
}

/** pill label with colored runs; returns its rect */
export function pill(
  ctx: CanvasRenderingContext2D,
  runs: readonly TextRun[],
  x: number,
  y: number,
  opts: { bg: string; border?: string; padX?: number; h?: number; align?: "left" | "center" | "right" },
) {
  const padX = opts.padX ?? 6;
  const h = opts.h ?? 18;
  const w = measureRuns(ctx, runs) + padX * 2;
  let left = x;
  if (opts.align === "center") left = x - w / 2;
  else if (opts.align === "right") left = x - w;
  left = Math.round(left);
  const top = Math.round(y - h / 2);
  roundRect(ctx, left + 0.5, top + 0.5, w - 1, h - 1, 4);
  ctx.fillStyle = opts.bg;
  ctx.fill();
  if (opts.border) {
    ctx.strokeStyle = opts.border;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  ctx.textBaseline = "middle";
  drawRuns(ctx, runs, left + padX, top + h / 2 + 0.5);
  return { left, top, w, h };
}
