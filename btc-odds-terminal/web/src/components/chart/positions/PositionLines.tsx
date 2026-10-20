"use client";
// TradingView-style position lines on the odds (left) scale. OWNER: chart-positions.
//  - entry: blue dashed line + triangle at each buy fill, label "UP 96.2 @ 26¢ +$59.64" (live P&L);
//  - TP green / SL red solid lines, labels "TP 96¢ +$67.33" (projected P&L at that price) with a drag grip
//    and an ✕; dragging writes store.setBracketDraft(positionId, …) live and debounces
//    api.putBracket(positionId, …); ✕ turns that leg off; "+TP" / "+SL" on the entry label adds a leg;
//  - before entry: the ticket's TP/SL (store.ticket, when bracketOn) as faint preview lines; dragging them
//    writes store.setTicket({ tpCents | slCents }).
// Canvas: PositionLinesPrimitive (attached to the odds scale). Labels / handles: LabelOverlay (HTML over the
// pane). Store + request wiring: PositionLinesController.

import { useEffect } from "react";
import { useChart, useChartPrimitive } from "../context";
import { PositionLinesController } from "./controller";
import { PositionLinesPrimitive } from "./PositionLinesPrimitive";

export function PositionLines() {
  const h = useChart();
  const prim = useChartPrimitive((handle) => new PositionLinesPrimitive(handle), "odds");
  useEffect(() => {
    const p = prim.current;
    if (!h || !p) return;
    const c = new PositionLinesController(h, p);
    return () => c.destroy();
  }, [h, prim]);
  return null;
}
