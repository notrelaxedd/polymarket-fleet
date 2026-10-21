// Title in the top-left corner of a pane, e.g. "EDGE · FAIR − MARKET  +2.4¢". Attach to any series of
// that pane; the engine sets `value` (hovered or latest) and calls update().

import { C, monoFont } from "../theme";
import { BasePrimitive, FnPaneView, FnRenderer, type MediaScope, type TextRun } from "./base";

export class PaneLabelPrimitive extends BasePrimitive {
  value: TextRun | null = null;
  private readonly views = [new FnPaneView(new FnRenderer((s) => this.draw(s)), "top")];

  constructor(
    h: ConstructorParameters<typeof BasePrimitive>[0],
    private readonly title: string,
  ) {
    super(h);
  }

  paneViews() {
    return this.views;
  }

  updateAllViews() {}

  private draw({ ctx }: MediaScope) {
    if (!this.enabled) return;
    ctx.font = monoFont(10, 600);
    ctx.textBaseline = "top";
    // letter-spaced uppercase label (canvas letterSpacing where supported)
    const c = ctx as CanvasRenderingContext2D & { letterSpacing?: string };
    c.letterSpacing = "0.6px";
    ctx.fillStyle = C.label;
    ctx.fillText(this.title, 10, 8);
    const w = ctx.measureText(this.title).width;
    c.letterSpacing = "0px";
    if (this.value) {
      ctx.fillStyle = this.value.color;
      ctx.fillText(this.value.text, 10 + w + 10, 8);
    }
  }
}
