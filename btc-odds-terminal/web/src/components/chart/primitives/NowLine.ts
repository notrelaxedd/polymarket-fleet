// "Now" marker: dotted vertical line at the server-corrected current time with "NOW · 6:45 LEFT" at the
// bottom of the main pane. The engine calls update() on a 1 Hz timer so it moves without new data.

import { serverNow, useTerminal } from "@/lib/store";
import { C, monoFont } from "../theme";
import { mss } from "../time";
import { BasePrimitive, FnPaneView, FnRenderer, pill, type MediaScope, type TextRun } from "./base";
import type { ReservedRect } from "./WindowBands";

const LABEL_CHAR_W = 6.1; // ~JetBrains Mono 10px advance, used before we have a context to measure

export class NowLinePrimitive extends BasePrimitive {
  private x: number | null = null;
  private runs: TextRun[] = [];
  /** the NOW pill's horizontal extent (for other bottom labels to avoid) */
  reserved: ReservedRect | null = null;
  private readonly views = [new FnPaneView(new FnRenderer((s) => this.draw(s)), "top")];

  paneViews() {
    return this.views;
  }

  updateAllViews() {
    this.x = null;
    this.reserved = null;
    if (!this.enabled) return;
    const now = serverNow();
    const x = this.h.timeToX(now);
    const width = this.chart?.timeScale().width() ?? 0;
    if (x == null || x < 0 || x > width) return;
    this.x = x;
    const cur = useTerminal.getState().current;
    this.runs = [{ text: "NOW", color: C.text2 }];
    if (cur && now < cur.endTs) {
      this.runs.push({ text: " · ", color: C.label }, { text: `${mss((cur.endTs - now) / 1000)} LEFT`, color: C.ptb });
    }
    const chars = this.runs.reduce((n, r) => n + r.text.length, 0);
    const w = chars * LABEL_CHAR_W + 14;
    const left = Math.min(Math.max(x - w / 2, 2), width - w - 2);
    this.reserved = { left, right: left + w };
  }

  private draw({ ctx, height, width }: MediaScope) {
    if (this.x == null) return;
    const x = Math.round(this.x) + 0.5;
    ctx.strokeStyle = C.nowLine;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = monoFont(10, 600);
    const r = this.reserved;
    const cx = r ? (r.left + r.right) / 2 : Math.min(Math.max(x, 40), width - 40);
    pill(ctx, this.runs, cx, height - 14, { bg: C.surface2, border: C.borderStrong, align: "center", h: 18, padX: 7 });
  }
}
