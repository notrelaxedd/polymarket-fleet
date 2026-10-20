"use client";
import * as React from "react";
import type { Bar } from "@shared/protocol";
import { useTerminal, type Timeframe } from "@/lib/store";
import { btc, cents } from "@/lib/format";
import { cn } from "@/lib/utils";

const TF_LABEL: Record<Timeframe, string> = { 1: "1s", 5: "5s", 15: "15s", 60: "1m", 300: "5m" };

/** Aggregates the tail of `bars` into the latest candle at timeframe `tf` seconds. */
function latestCandle(bars: Bar[], tf: number): { o: number; h: number; l: number; c: number } | null {
  const last = bars[bars.length - 1];
  if (!last) return null;
  const span = tf * 1000;
  const bucket = Math.floor(last.t / span) * span;
  let o = last.o;
  let h = last.h;
  let l = last.l;
  const c = last.c;
  for (let i = bars.length - 2; i >= 0 && bars[i].t >= bucket; i--) {
    const b = bars[i];
    o = b.o;
    h = Math.max(h, b.h);
    l = Math.min(l, b.l);
  }
  return { o, h, l, c };
}

function Item({ k, v, color }: { k: string; v: string; color?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1 whitespace-nowrap">
      <span className="label !text-[11px]">{k}</span>
      <span className="num text-[12px] font-medium" style={{ color }}>
        {v}
      </span>
    </span>
  );
}

export function LegendRow() {
  const timeframe = useTerminal((s) => s.timeframe);
  const hover = useTerminal((s) => s.hover);
  useTerminal((s) => s.version); // re-render when series change
  const bars = useTerminal.getState().series.bars;
  const lastOdds = useTerminal((s) => s.lastOdds);
  const lastFair = useTerminal((s) => s.lastFair);
  const ptbCur = useTerminal((s) => s.current?.priceToBeat ?? null);

  const candle = hover ? { o: hover.o, h: hover.h, l: hover.l, c: hover.c } : latestCandle(bars, timeframe);
  const up = candle ? candle.c >= candle.o : true;
  const ohlcColor = up ? "#34D399" : "#F87171";
  const upMid = hover ? hover.upMid : (lastOdds?.upMid ?? null);
  const fair = hover ? hover.fair : (lastFair?.fair ?? null);
  const ptb = hover ? hover.ptb : ptbCur;

  return (
    <div className="flex items-center gap-x-4 overflow-x-auto border-b border-border bg-surface px-3 py-1.5" aria-label="Chart legend">
      <span className="whitespace-nowrap text-[12px] font-semibold text-text-2">
        BTC · BRTI proxy <span className="font-normal text-muted">(median CB/KR/BS)</span> · <span className="num">{TF_LABEL[timeframe]}</span>
      </span>
      <span className={cn("flex items-center gap-x-3", !candle && "opacity-60")}>
        <Item k="O" v={candle ? btc(candle.o) : "—"} color={ohlcColor} />
        <Item k="H" v={candle ? btc(candle.h) : "—"} color={ohlcColor} />
        <Item k="L" v={candle ? btc(candle.l) : "—"} color={ohlcColor} />
        <Item k="C" v={candle ? btc(candle.c) : "—"} color={ohlcColor} />
      </span>
      <Item k="Up mid" v={cents(upMid, 1)} color="#7FB2FF" />
      <Item k="Fair" v={cents(fair, 1)} color="#C29BFF" />
      <Item k="PTB" v={ptb != null ? `$${btc(ptb)}` : "—"} color="#F5B942" />
    </div>
  );
}
