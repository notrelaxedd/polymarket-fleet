// Series primitive that paints every drawing + sticker on the main pane and exposes the projected shapes
// for hit testing. Attached to the BTC (right-scale) anchor series; odds-scale drawings convert through the
// left scale. A companion `DrawingsAxisPrimitive` on the odds series carries the left-axis tags.
//
// Scene input comes from the DrawingController (store list + in-progress drag/create preview).

import type { ISeriesPrimitiveAxisView, PrimitiveHoveredItem } from "lightweight-charts";
import type { Drawing, DrawingScale } from "@shared/protocol";
import { STICKERS } from "@/components/stickers";
import type { ChartHandle } from "../context";
import { fmtBtc, fmtCents } from "../fmt";
import { AxisTag, BasePrimitive, FnPaneView, FnRenderer, roundRect, type MediaScope } from "../primitives/base";
import { contrastText } from "../primitives/LastValue";
import { C, monoFont } from "../theme";
import { hms } from "../time";
import { Coords } from "./coords";
import {
  fibLevels,
  fmtDuration,
  HANDLE_R,
  handlesOf,
  hitTestAll,
  measureStats,
  normBox,
  rayEnd,
  shapeBox,
  wrapText,
  type Box,
  type Hit,
  type Projected,
  type View,
  type XY,
} from "./geometry";
import { TEXT_PLACEHOLDER, validDrawings, withAlpha } from "./model";
import { stickerImage } from "./stickerArt";

export interface DrawingScene {
  drawings: readonly Drawing[];
  selectedId: string | null;
  hoverId: string | null;
  /** text note / line label currently open in the HTML editor (its canvas text is hidden) */
  editingId: string | null;
  /** global lock from the tools rail */
  locked: boolean;
  /** cursor tool active: drawings are hoverable / grabbable */
  interactive: boolean;
}

export const STICKER_R = 14;
const TEXT_MAX_W = 220;
const TEXT_PAD_X = 8;
const TEXT_PAD_Y = 6;
const TEXT_LINE_H = 16;
const MEASURE_LINE_H = 15;

// ---- fonts ------------------------------------------------------------------------------------------

const SANS_FALLBACK = `Inter, system-ui, -apple-system, "Segoe UI", sans-serif`;
let sansFamily = SANS_FALLBACK;
export function resolveSansFont(el?: Element | null) {
  if (typeof window === "undefined") return;
  for (const t of [el, document.body, document.documentElement]) {
    if (!t) continue;
    const v = getComputedStyle(t).getPropertyValue("--font-inter").trim();
    if (v) {
      sansFamily = `${v}, ${SANS_FALLBACK}`;
      return;
    }
  }
}
export const sansFont = (px: number, weight = 500) => `${weight} ${px}px ${sansFamily}`;

let measureCtx: CanvasRenderingContext2D | null = null;
function measurer(): CanvasRenderingContext2D | null {
  if (measureCtx || typeof document === "undefined") return measureCtx;
  measureCtx = document.createElement("canvas").getContext("2d");
  return measureCtx;
}

const textCache = new Map<string, { lines: string[]; w: number }>();
function layoutText(text: string): { lines: string[]; w: number } {
  const font = sansFont(12);
  const key = `${font}|${text}`;
  let v = textCache.get(key);
  if (v) return v;
  const ctx = measurer();
  const m = (s: string) => (ctx ? ctx.measureText(s).width : s.length * 6.5);
  if (ctx) ctx.font = font;
  const lines = wrapText(text, TEXT_MAX_W, m);
  v = { lines, w: Math.max(...lines.map(m), 24) };
  if (textCache.size > 300) textCache.clear();
  textCache.set(key, v);
  return v;
}

function textWidth(s: string, font: string): number {
  const ctx = measurer();
  if (!ctx) return s.length * 6.5;
  ctx.font = font;
  return ctx.measureText(s).width;
}

export const fmtPrice = (price: number, scale: DrawingScale) => (scale === "odds" ? fmtCents(price) : fmtBtc(price));

/** text shown on a horizontal line's inline label */
export function hlineLabel(d: Drawing): { label: string | null; price: string } {
  const p = d.points[0];
  return { label: d.label?.trim() ? d.label.trim() : null, price: fmtPrice(p.price, p.scale) };
}

export function measureLabel(d: Drawing, tfSec: number): { main: string; sub: string; up: boolean } {
  const [p0, p1] = d.points;
  const s = measureStats(p0, p1, tfSec);
  const sign = s.delta >= 0 ? "+" : "−";
  const abs = Math.abs(s.delta);
  const main =
    p0.scale === "odds"
      ? `${sign}${abs.toFixed(1)}¢`
      : `${sign}${fmtBtc(abs)} (${sign}${Math.abs(s.pct ?? 0).toFixed(2)}%)`;
  return { main, sub: `${s.bars} ${s.bars === 1 ? "bar" : "bars"} · ${fmtDuration(s.durationMs)}`, up: s.up };
}

// ---- axis tags --------------------------------------------------------------------------------------

interface TagSpec {
  y: number;
  label: string;
  bg: string;
  fg: string;
}

/** price-axis tags for one scale: every horizontal line, plus the selected drawing's anchor prices */
function priceTags(scene: DrawingScene, scale: DrawingScale, coords: Coords): TagSpec[] {
  const out: TagSpec[] = [];
  for (const d of scene.drawings) {
    const sel = d.id === scene.selectedId;
    if (d.kind === "hline") {
      const p = d.points[0];
      if (p.scale !== scale) continue;
      const y = coords.priceToY(p.price, scale);
      if (y != null) out.push({ y, label: fmtPrice(p.price, scale), bg: d.color, fg: contrastText(d.color) });
    } else if (sel && d.kind !== "vline" && d.kind !== "sticker" && d.kind !== "text") {
      for (const p of d.points) {
        if (p.scale !== scale) continue;
        const y = coords.priceToY(p.price, scale);
        if (y != null) out.push({ y, label: fmtPrice(p.price, scale), bg: C.borderStrong, fg: C.text });
      }
    }
  }
  return out;
}

/** Reusable axis tags. lightweight-charts caches primitive axis views by array identity, so the array is
 *  replaced (never mutated) whenever the pool grows. */
class TagPool {
  private tags: AxisTag[] = [];
  views(): readonly ISeriesPrimitiveAxisView[] {
    return this.tags;
  }
  set(specs: TagSpec[]) {
    if (this.tags.length < specs.length) {
      const grown = [...this.tags];
      while (grown.length < specs.length) grown.push(new AxisTag());
      this.tags = grown;
    }
    this.tags.forEach((t, i) => {
      const s = specs[i];
      t.show = !!s;
      if (!s) return;
      t.y = s.y;
      t.label = s.label;
      t.bgColor = s.bg;
      t.fg = s.fg;
    });
  }
}

// ---- primitive --------------------------------------------------------------------------------------

export class DrawingsPrimitive extends BasePrimitive {
  readonly coords: Coords;
  /** shapes projected in the last updateAllViews, in paint order */
  shapes: Projected[] = [];
  viewSize: View = { width: 0, height: 0 };
  getScene: () => DrawingScene = () => ({ drawings: [], selectedId: null, hoverId: null, editingId: null, locked: false, interactive: true });
  /** called after every layout with the selected drawing's screen box (toolbar positioning) */
  onLayout: ((selected: { d: Drawing; box: Box } | null) => void) | null = null;

  private scene: DrawingScene = this.getScene();
  private readonly priceTags = new TagPool();
  private readonly timeTags = new TagPool();
  private readonly views = [
    new FnPaneView(new FnRenderer((s) => this.drawShapes(s)), "normal"),
    new FnPaneView(new FnRenderer((s) => this.drawOverlay(s)), "top"),
  ];
  private readonly repaint = () => this.update();

  constructor(h: ChartHandle) {
    super(h);
    this.coords = new Coords(h);
    resolveSansFont(h.container);
  }

  paneViews() {
    return this.views;
  }
  priceAxisViews() {
    return this.priceTags.views();
  }
  timeAxisViews() {
    return this.timeTags.views();
  }

  /** chart-driven hover hit test: only supplies the cursor (the controller handles pointer input) */
  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const scene = this.scene;
    if (!scene.interactive) return null;
    const hit = this.hit({ x, y }, 6);
    if (!hit) return null;
    const d = scene.drawings.find((z) => z.id === hit.id);
    if (!d) return null;
    const locked = d.locked || scene.locked;
    let cursorStyle = "pointer";
    // an unselected drawing's fill only selects on click (the chart keeps pan / zoom): no move cursor
    if (hit.fill && d.id !== scene.selectedId) return { externalId: `drawing:${d.id}`, zOrder: "normal", cursorStyle, hitTestPriority: 0 };
    if (!locked) {
      if (hit.part !== "body") cursorStyle = "grab";
      else cursorStyle = d.kind === "hline" ? "ns-resize" : d.kind === "vline" ? "ew-resize" : "move";
    }
    return { externalId: `drawing:${d.id}`, zOrder: "top", cursorStyle, hitTestPriority: hit.part === "body" ? 1 : 2 };
  }

  hit(p: XY, tol: number): Hit | null {
    return hitTestAll(this.shapes, p, tol, this.viewSize, this.scene.selectedId);
  }

  updateAllViews() {
    const raw = this.getScene();
    // malformed drawings (server rows, undo stacks) never reach the painter: one bad row would throw inside
    // lightweight-charts' paint and blank the whole pane
    this.scene = { ...raw, drawings: validDrawings(raw.drawings) };
    const scene = this.scene;
    const view = this.coords.view();
    this.viewSize = view;
    const shapes: Projected[] = [];
    for (const d of scene.drawings) {
      try {
        const pts: XY[] = [];
        for (const p of d.points) {
          const xy = this.coords.toXY(p);
          if (!xy) break;
          pts.push(xy);
        }
        if (pts.length < d.points.length || !pts.length) continue;
        const sh: Projected = { d, pts, box: this.boxFor(d, pts, view) };
        if (d.kind === "fib") sh.levelYs = this.fibYs(d);
        shapes.push(sh);
      } catch {
        // skip this drawing, keep the rest
      }
    }
    this.shapes = shapes;

    this.priceTags.set(safeTags(() => priceTags(scene, "btc", this.coords)));
    const sel = scene.selectedId ? shapes.find((s) => s.d.id === scene.selectedId) : undefined;
    const tt: TagSpec[] = [];
    if (sel && sel.d.kind !== "hline") {
      for (let i = 0; i < sel.pts.length; i++) {
        const x = sel.pts[i].x;
        if (x < 0 || x > view.width) continue;
        tt.push({ y: x, label: hms(sel.d.points[i].time), bg: C.borderStrong, fg: C.text });
      }
    }
    this.timeTags.set(tt);

    if (this.onLayout) {
      const box = sel ? shapeBox(sel, view) : null;
      this.onLayout(sel && box ? { d: sel.d, box } : null);
    }
  }

  private fibYs(d: Drawing): number[] {
    const [p0, p1] = d.points;
    return fibLevels(p0.price, p1.price)
      .map((l) => this.coords.priceToY(l.price, p0.scale))
      .filter((y): y is number => y != null);
  }

  /** layout boxes used for painting + hit testing */
  private boxFor(d: Drawing, pts: XY[], view: View): Box | null {
    const a = pts[0];
    switch (d.kind) {
      case "text": {
        const t = layoutText(d.label?.trim() ? d.label : TEXT_PLACEHOLDER);
        const w = Math.ceil(t.w) + TEXT_PAD_X * 2;
        const h = t.lines.length * TEXT_LINE_H + TEXT_PAD_Y * 2;
        return { left: a.x, top: a.y, right: a.x + w, bottom: a.y + h };
      }
      case "sticker":
        return { left: a.x - STICKER_R, top: a.y - STICKER_R, right: a.x + STICKER_R, bottom: a.y + STICKER_R };
      case "hline": {
        const { label, price } = hlineLabel(d);
        const w = (label ? textWidth(label, sansFont(11, 500)) + 6 : 0) + textWidth(price, monoFont(10, 600)) + 12;
        const right = view.width - 8;
        return { left: right - w, top: a.y - 9, right, bottom: a.y + 9 };
      }
      case "measure": {
        const b = pts[1];
        const { main, sub } = measureLabel(d, this.h.timeframe());
        const w = Math.max(textWidth(main, monoFont(11, 600)), textWidth(sub, monoFont(10, 500))) + 16;
        const h = MEASURE_LINE_H * 2 + 10;
        const bx = normBox(a, b);
        const cx = (bx.left + bx.right) / 2;
        // label on the side the measure points to (below for a drop, above for a rise)
        const below = b.y >= a.y;
        let top = below ? bx.bottom + 6 : bx.top - 6 - h;
        if (top < 2) top = bx.bottom + 6;
        if (top + h > view.height - 2) top = Math.max(2, bx.top - 6 - h);
        return { left: cx - w / 2, top, right: cx + w / 2, bottom: top + h };
      }
      default:
        return null;
    }
  }

  // ---- painting -------------------------------------------------------------------------------------

  private drawShapes({ ctx, width, height }: MediaScope) {
    const scene = this.scene;
    const view = { width, height };
    for (const sh of this.shapes) {
      const d = sh.d;
      const hot = d.id === scene.hoverId || d.id === scene.selectedId;
      const lw = (d.lineWidth ?? 1.5) + (hot ? 0.5 : 0);
      ctx.save();
      try {
        this.paintShape(ctx, sh, view, lw, hot);
      } catch {
        // one drawing failing to paint must not take down the series pane
      } finally {
        ctx.restore();
      }
    }
  }

  private paintShape(ctx: CanvasRenderingContext2D, sh: Projected, view: View, lw: number, hot: boolean) {
    const scene = this.scene;
    const d = sh.d;
    const { width, height } = view;
    ctx.strokeStyle = d.color;
    ctx.fillStyle = d.color;
    ctx.lineWidth = lw;
    ctx.lineCap = "round";
    ctx.setLineDash(d.dashed ? [6, 4] : []);
    switch (d.kind) {
      case "trend":
        line(ctx, sh.pts[0], sh.pts[1]);
        break;
      case "ray":
        line(ctx, sh.pts[0], rayEnd(sh.pts[0], sh.pts[1], view));
        break;
      case "hline":
        this.drawHline(ctx, sh, width, lw, hot);
        break;
      case "vline": {
        const x = Math.round(sh.pts[0].x) + 0.5;
        line(ctx, { x, y: 0 }, { x, y: height });
        break;
      }
      case "rect": {
        const b = normBox(sh.pts[0], sh.pts[1]);
        ctx.fillStyle = withAlpha(d.color, hot ? 0.16 : 0.1);
        ctx.fillRect(b.left, b.top, b.right - b.left, b.bottom - b.top);
        ctx.strokeRect(Math.round(b.left) + 0.5, Math.round(b.top) + 0.5, Math.round(b.right - b.left), Math.round(b.bottom - b.top));
        break;
      }
      case "fib":
        this.drawFib(ctx, sh, lw);
        break;
      case "measure":
        this.drawMeasure(ctx, sh);
        break;
      case "text":
        if (d.id !== scene.editingId) this.drawText(ctx, sh, d.id === scene.selectedId);
        break;
      case "sticker":
        this.drawSticker(ctx, sh, hot);
        break;
    }
  }

  /** selection handles + outlines (above everything else in the pane) */
  private drawOverlay({ ctx, width, height }: MediaScope) {
    const scene = this.scene;
    const sel = scene.selectedId ? this.shapes.find((s) => s.d.id === scene.selectedId) : undefined;
    if (!sel) return;
    ctx.save();
    try {
      this.paintSelection(ctx, sel, width, height);
    } catch {
      // ignore: handles are cosmetic
    } finally {
      ctx.restore();
    }
  }

  private paintSelection(ctx: CanvasRenderingContext2D, sel: Projected, width: number, height: number) {
    const scene = this.scene;
    const locked = sel.d.locked || scene.locked;
    if ((sel.d.kind === "text" || sel.d.kind === "sticker") && sel.box) {
      const b = sel.box;
      ctx.strokeStyle = C.odds;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      if (sel.d.kind === "sticker") {
        ctx.beginPath();
        ctx.arc(sel.pts[0].x, sel.pts[0].y, STICKER_R + 3.5, 0, Math.PI * 2);
        ctx.stroke();
      } else {
        roundRect(ctx, b.left - 3.5, b.top - 3.5, b.right - b.left + 7, b.bottom - b.top + 7, 8);
        ctx.stroke();
      }
      ctx.setLineDash([]);
    }
    for (const h of handlesOf(sel, { width, height })) {
      ctx.beginPath();
      ctx.arc(h.at.x, h.at.y, HANDLE_R, 0, Math.PI * 2);
      ctx.fillStyle = C.bg;
      ctx.fill();
      ctx.lineWidth = 1.75;
      ctx.strokeStyle = locked ? C.label : sel.d.color;
      ctx.stroke();
    }
  }

  private drawHline(ctx: CanvasRenderingContext2D, sh: Projected, width: number, lw: number, hot: boolean) {
    const d = sh.d;
    const y = Math.round(sh.pts[0].y) + (Math.round(lw) % 2 ? 0.5 : 0);
    const box = sh.box;
    ctx.setLineDash(d.dashed ? [6, 4] : [1.5, 3]);
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(box ? box.left - 2 : width, y);
    if (box) {
      ctx.moveTo(box.right + 2, y);
      ctx.lineTo(width, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
    if (!box || box.left < 4) return;
    // inline label: "[label] price" in a dark pill sitting on the line
    const { label, price } = hlineLabel(d);
    roundRect(ctx, box.left + 0.5, box.top + 0.5, box.right - box.left - 1, box.bottom - box.top - 1, 4);
    ctx.fillStyle = "rgba(10, 13, 18, 0.92)";
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = withAlpha(d.color, hot ? 0.8 : 0.45);
    ctx.stroke();
    ctx.textBaseline = "middle";
    const cy = (box.top + box.bottom) / 2 + 0.5;
    let x = box.left + 6;
    if (label) {
      ctx.font = sansFont(11, 500);
      ctx.fillStyle = C.text2;
      ctx.fillText(label, x, cy);
      x += ctx.measureText(label).width + 6;
    }
    ctx.font = monoFont(10, 600);
    ctx.fillStyle = d.color;
    ctx.fillText(price, x, cy);
  }

  private drawFib(ctx: CanvasRenderingContext2D, sh: Projected, lw: number) {
    const d = sh.d;
    const [a, b] = sh.pts;
    const [p0, p1] = d.points;
    const left = Math.min(a.x, b.x);
    const right = Math.max(a.x, b.x);
    const levels = fibLevels(p0.price, p1.price)
      .map((l) => ({ ...l, y: this.coords.priceToY(l.price, p0.scale) }))
      .filter((l): l is typeof l & { y: number } => l.y != null);
    // alternating faint fills between levels
    for (let i = 0; i + 1 < levels.length; i++) {
      const y1 = levels[i].y;
      const y2 = levels[i + 1].y;
      ctx.fillStyle = withAlpha(d.color, i % 2 ? 0.05 : 0.09);
      ctx.fillRect(left, Math.min(y1, y2), right - left, Math.abs(y2 - y1));
    }
    ctx.lineWidth = Math.max(1, lw - 0.5);
    ctx.lineCap = "butt";
    for (const l of levels) {
      const y = Math.round(l.y) + 0.5;
      ctx.strokeStyle = l.level === 0 || l.level === 1 ? d.color : withAlpha(d.color, 0.75);
      line(ctx, { x: left, y }, { x: right, y });
    }
    // trend connecting the two anchors
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = withAlpha(d.color, 0.5);
    ctx.lineWidth = 1;
    line(ctx, a, b);
    ctx.setLineDash([]);
    if (right - left < 60) return;
    ctx.font = monoFont(10, 500);
    ctx.textBaseline = "bottom";
    ctx.fillStyle = d.color;
    for (const l of levels) ctx.fillText(`${l.level} (${fmtPrice(l.price, p0.scale)})`, left + 4, Math.round(l.y) - 2);
  }

  private drawMeasure(ctx: CanvasRenderingContext2D, sh: Projected) {
    const d = sh.d;
    const [a, b] = sh.pts;
    const { main, sub, up } = measureLabel(d, this.h.timeframe());
    const col = up ? C.up : C.down;
    const bx = normBox(a, b);
    ctx.fillStyle = withAlpha(col, 0.14);
    ctx.fillRect(bx.left, bx.top, bx.right - bx.left, bx.bottom - bx.top);
    ctx.strokeStyle = col;
    ctx.fillStyle = col;
    ctx.lineWidth = 1.25;
    const cx = Math.round((bx.left + bx.right) / 2) + 0.5;
    const cy = Math.round((bx.top + bx.bottom) / 2) + 0.5;
    arrow(ctx, { x: cx, y: a.y }, { x: cx, y: b.y });
    if (Math.abs(b.x - a.x) > 12) arrow(ctx, { x: a.x, y: cy }, { x: b.x, y: cy });
    const box = sh.box;
    if (!box) return;
    roundRect(ctx, box.left + 0.5, box.top + 0.5, box.right - box.left - 1, box.bottom - box.top - 1, 6);
    ctx.fillStyle = "rgba(21, 27, 36, 0.96)";
    ctx.fill();
    ctx.strokeStyle = withAlpha(col, 0.55);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const mid = (box.left + box.right) / 2;
    ctx.font = monoFont(11, 600);
    ctx.fillStyle = col;
    ctx.fillText(main, mid, box.top + 5 + MEASURE_LINE_H / 2 + 0.5);
    ctx.font = monoFont(10, 500);
    ctx.fillStyle = C.text2;
    ctx.fillText(sub, mid, box.top + 5 + MEASURE_LINE_H * 1.5 + 0.5);
    ctx.textAlign = "left";
  }

  private drawText(ctx: CanvasRenderingContext2D, sh: Projected, selected: boolean) {
    const d = sh.d;
    const box = sh.box;
    if (!box) return;
    const empty = !d.label?.trim();
    const t = layoutText(empty ? TEXT_PLACEHOLDER : (d.label as string));
    roundRect(ctx, box.left + 0.5, box.top + 0.5, box.right - box.left - 1, box.bottom - box.top - 1, 6);
    ctx.fillStyle = "rgba(21, 27, 36, 0.96)";
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = selected ? withAlpha(d.color, 0.9) : withAlpha(d.color, 0.38);
    ctx.stroke();
    ctx.font = sansFont(12);
    ctx.textBaseline = "middle";
    ctx.fillStyle = empty ? C.label : d.color;
    t.lines.forEach((ln, i) => ctx.fillText(ln, box.left + TEXT_PAD_X, box.top + TEXT_PAD_Y + TEXT_LINE_H * (i + 0.5) + 0.5));
  }

  private drawSticker(ctx: CanvasRenderingContext2D, sh: Projected, hot: boolean) {
    const kind = sh.d.sticker ?? "target";
    const { x, y } = sh.pts[0];
    ctx.shadowColor = "rgba(0, 0, 0, 0.45)";
    ctx.shadowBlur = hot ? 8 : 4;
    ctx.shadowOffsetY = 1;
    const img = stickerImage(kind, this.repaint);
    if (img) ctx.drawImage(img, x - STICKER_R, y - STICKER_R, STICKER_R * 2, STICKER_R * 2);
    else {
      ctx.beginPath();
      ctx.arc(x, y, STICKER_R, 0, Math.PI * 2);
      ctx.fillStyle = STICKERS[kind]?.color ?? C.borderStrong;
      ctx.fill();
    }
  }
}

/** Left-axis tags (odds-scale horizontal lines + selected odds-scale anchors). Attached to the odds series. */
export class DrawingsAxisPrimitive extends BasePrimitive {
  private readonly coords: Coords;
  private readonly tags = new TagPool();
  getScene: () => DrawingScene | null = () => null;
  constructor(h: ChartHandle) {
    super(h);
    this.coords = new Coords(h);
  }
  paneViews() {
    return [];
  }
  priceAxisViews() {
    return this.tags.views();
  }
  updateAllViews() {
    const scene = this.getScene();
    this.tags.set(scene ? safeTags(() => priceTags({ ...scene, drawings: validDrawings(scene.drawings) }, "odds", this.coords)) : []);
  }
}

// ---- canvas helpers ---------------------------------------------------------------------------------

function safeTags(f: () => TagSpec[]): TagSpec[] {
  try {
    return f();
  } catch {
    return [];
  }
}

function line(ctx: CanvasRenderingContext2D, a: XY, b: XY) {
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();
}

function arrow(ctx: CanvasRenderingContext2D, a: XY, b: XY) {
  line(ctx, a, b);
  const len = Math.hypot(b.x - a.x, b.y - a.y);
  if (len < 8) return;
  const ang = Math.atan2(b.y - a.y, b.x - a.x);
  const s = 6;
  ctx.beginPath();
  ctx.moveTo(b.x, b.y);
  ctx.lineTo(b.x - s * Math.cos(ang - 0.45), b.y - s * Math.sin(ang - 0.45));
  ctx.lineTo(b.x - s * Math.cos(ang + 0.45), b.y - s * Math.sin(ang + 0.45));
  ctx.closePath();
  ctx.fill();
}
