"use client";
// Card 2: Lag: odds vs BTC. Cross-correlation of BTC returns with later Up-odds changes at lags 0–30s (2s
// steps); the peak bar is purple with the label "4s · ρ 0.61". OWNER: tabs.
import * as React from "react";
import type { AnalysisPayload, LagBar } from "@shared/protocol";
import { LAG_STEPS_SEC } from "@shared/analysis";
import { AXIS_FONT, AXIS_TEXT, BoxSwatch, PairCard, PlotNote } from "./frame";
import { PAIR_COLORS as C, lagDomain, lagPeakLabel, linear, niceTicks, resolveLagPeak } from "./geom";

const M = { l: 32, r: 8, t: 16, b: 16 };

const EMPTY_BARS: LagBar[] = LAG_STEPS_SEC.map((lagSec) => ({ lagSec, rho: null, n: 0 }));

export function LagCard({ data }: { data: AnalysisPayload | null }) {
  const bars = data?.lag.length ? data.lag : EMPTY_BARS;
  const peak = resolveLagPeak(bars, data?.lagPeak ?? null);
  const peakLabel = lagPeakLabel(peak);
  return (
    <PairCard
      title="Lag: odds vs BTC"
      info="Correlation (ρ) between each second's BTC return and the change in the Up mid that many seconds later. The peak shows how long the market takes to react."
      legend={[
        { key: "rho", label: "ρ by lag", swatch: <BoxSwatch color={C.lagBar} /> },
        { key: "peak", label: <span>Peak{peakLabel ? <span className="num"> {peakLabel}</span> : null}</span>, swatch: <BoxSwatch color={C.fair} /> },
      ]}
      takeaway={data?.takeaways.lag ?? null}
      plotLabel={
        peakLabel
          ? `Bar chart of correlation by lag from 0 to 30 seconds. Peak at ${peakLabel.replace("ρ", "rho")}.`
          : "Bar chart of correlation by lag from 0 to 30 seconds. Not enough data yet."
      }
    >
      {(w, h) => <LagPlot bars={bars} peak={peak} peakLabel={peakLabel} loading={!data} w={w} h={h} />}
    </PairCard>
  );
}

function LagPlot({
  bars,
  peak,
  peakLabel,
  loading,
  w,
  h,
}: {
  bars: LagBar[];
  peak: { lagSec: number; rho: number } | null;
  peakLabel: string | null;
  loading: boolean;
  w: number;
  h: number;
}) {
  const x0 = M.l;
  const x1 = w - M.r;
  const y0 = M.t;
  const y1 = h - M.b;
  const [lo, hi] = lagDomain(bars);
  const y = linear(lo, hi, y1, y0);
  const maxLag = Math.max(30, ...bars.map((b) => b.lagSec));
  const slot = (x1 - x0) / (maxLag / 2 + 1);
  const bw = Math.max(3, Math.min(18, slot * 0.64));
  const cx = (lag: number) => x0 + slot * (lag / 2 + 0.5);
  const ticks = niceTicks(lo, hi, 2).filter((t) => t >= lo - 1e-9 && t <= hi + 1e-9);
  const hasData = bars.some((b) => b.rho != null);
  const zero = y(0);

  let labelX = 0;
  let labelY = 0;
  if (peak && peakLabel) {
    const px = cx(peak.lagSec);
    const half = peakLabel.length * 3.1 + 2; // ~6.2px per mono glyph at 10px
    labelX = Math.min(x1 - half, Math.max(x0 + half, px));
    labelY = Math.max(10, (peak.rho >= 0 ? y(peak.rho) : zero) - 4);
  }

  return (
    <svg width={w} height={h} aria-hidden className="block overflow-visible">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x0} x2={x1} y1={y(t)} y2={y(t)} stroke={t === 0 ? C.gridStrong : C.grid} />
          <text x={x0 - 4} y={y(t)} textAnchor="end" dominantBaseline="middle" {...AXIS_TEXT}>
            {t === 0 ? "0" : `${t < 0 ? "−" : ""}${+Math.abs(t).toFixed(2)}`}
          </text>
        </g>
      ))}
      {!ticks.includes(0) && <line x1={x0} x2={x1} y1={zero} y2={zero} stroke={C.gridStrong} />}
      <text x={x0 - 4} y={y0 - 9} textAnchor="end" {...AXIS_TEXT}>
        ρ
      </text>

      {bars.map((b) => {
        const isPeak = peak != null && b.lagSec === peak.lagSec;
        const x = cx(b.lagSec) - bw / 2;
        if (b.rho == null || !Number.isFinite(b.rho)) {
          return <line key={b.lagSec} x1={x} x2={x + bw} y1={zero} y2={zero} stroke="#4A5465" strokeWidth={2} />;
        }
        const yt = Math.min(zero, y(b.rho));
        const hgt = Math.max(1, Math.abs(y(b.rho) - zero));
        return (
          <rect key={b.lagSec} x={x} y={yt} width={bw} height={hgt} rx={1.5} fill={isPeak ? C.fair : C.lagBar}>
            <title>{`${b.lagSec}s · ρ ${b.rho.toFixed(2)} · n ${b.n}`}</title>
          </rect>
        );
      })}

      {[0, 10, 20, 30].filter((l) => l <= maxLag).map((l) => (
        <text key={l} x={cx(l)} y={h - 3} textAnchor="middle" {...AXIS_TEXT}>
          {l}s
        </text>
      ))}

      {peak && peakLabel && (
        <text x={labelX} y={labelY} textAnchor="middle" fontSize={AXIS_FONT} fontWeight={600} fill={C.fair} className="font-mono">
          {peakLabel}
        </text>
      )}
      {!hasData && <PlotNote x={(x0 + x1) / 2} y={(y0 + y1) / 2 - 6}>{loading ? "Loading…" : "Not enough data yet"}</PlotNote>}
    </svg>
  );
}
