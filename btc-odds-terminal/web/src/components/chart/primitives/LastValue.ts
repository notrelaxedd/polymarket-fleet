// Live-value tag on a price axis (current BTC on the right, current Up mid on the left).
// Replaces the series' built-in last-value label, which lightweight-charts derives from the last bar in
// the VISIBLE range — after scrolling back it would show a stale price. This one always reads the
// latest aggregated bucket. Optionally steps out of the way of another tag's y (the PTB tag).

import { AxisTag, BasePrimitive } from "./base";

export interface LastValue {
  price: number;
  label: string;
  bg: string;
}

/** Approximate height of a price-axis label at the chart's 11px font (used to dodge another tag). */
export const AXIS_TAG_H = 19;

/** Text color for a tag background, using the same luminance rule lightweight-charts uses for its labels. */
export function contrastText(bg: string): string {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(bg);
  if (!m) return "#FFFFFF";
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => parseInt(h, 16));
  return 0.199 * r + 0.687 * g + 0.114 * b > 160 ? "#0A0D12" : "#FFFFFF";
}

/** Tag y after dodging a fixed label at `avoid` (keeps both readable; the series' price line stays exact). */
export function dodge(y: number, avoid: number | null, h = AXIS_TAG_H): number {
  if (avoid == null || Math.abs(y - avoid) >= h) return y;
  return y < avoid ? avoid - h : avoid + h;
}

export class LastValuePrimitive extends BasePrimitive {
  private readonly tag = new AxisTag();
  private readonly tags = [this.tag];
  /** latest value to show, or null to hide */
  getValue: () => LastValue | null = () => null;
  /** y of a fixed tag on the same scale to step away from */
  getAvoidY: () => number | null = () => null;

  paneViews() {
    return [];
  }
  priceAxisViews() {
    return this.tags;
  }

  updateAllViews() {
    this.tag.show = false;
    if (!this.enabled || !this.series) return;
    const v = this.getValue();
    if (!v) return;
    const y = this.series.priceToCoordinate(v.price);
    if (y == null) return;
    this.tag.y = dodge(y, this.getAvoidY());
    this.tag.tick = this.tag.y === y;
    this.tag.label = v.label;
    this.tag.bgColor = v.bg;
    this.tag.fg = contrastText(v.bg);
    this.tag.show = true;
  }
}
