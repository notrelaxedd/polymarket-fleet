"use client";
// Card 1: BTC vs strike -> Up price. x = ln(S/K)/(σ√t) in σ units (−2..+2), y = Up mid 0–100¢; points colored by
// time left (4 steps, dark slate -> light blue), dashed purple fair curve for the current time left, the current
// sample as an amber ring + "now". OWNER: tabs.
import * as React from "react";
import type { AnalysisPayload } from "@shared/protocol";
import { mSS } from "@/lib/format";
import { AXIS_FONT, AXIS_TEXT, DotSwatch, LineSwatch, PairCard, PlotNote, RingSwatch } from "./frame";
import { PAIR_COLORS as C, TLEFT_BUCKETS, Z_MAX, Z_MIN, dotsPath, linePath, linear, scatterLayers } from "./geom";

const M = { l: 28, r: 8, t: 6, b: 16 };

export function ScatterCard({ data }: { data: AnalysisPayload | null }) {
  const cur = data?.current ?? null;
  const n = data?.scatter.length ?? 0;
  const legend = [
    {
      key: "tleft",
      label: (
        <span>
          Time left <span className="num">{TLEFT_BUCKETS[0].label}→{TLEFT_BUCKETS[TLEFT_BUCKETS.length - 1].label}</span>
        </span>
      ),
      swatch: (
        <span className="inline-flex gap-[2px]" title={TLEFT_BUCKETS.map((b) => b.label).join(" · ")}>
          {TLEFT_BUCKETS.map((b) => (
            <DotSwatch key={b.label} color={b.color} />
          ))}
        </span>
      ),
    },
    {
      key: "fair",
      label: <span>Fair{cur ? <span className="num"> @ {mSS(cur.tLeftSec)}</span> : null}</span>,
      swatch: <LineSwatch color={C.fair} dashed />,
    },
    { key: "now", label: <span>now</span>, swatch: <RingSwatch color={C.ptb} /> },
  ];
  const plotLabel = cur
    ? `Scatter of ${n} samples. Now: ${cur.z.toFixed(2)} sigma from the price to beat, Up ${cur.upMid.toFixed(1)} cents${
        cur.fair != null ? `, fair ${cur.fair.toFixed(1)} cents` : ""
      }.`
    : `Scatter of ${n} samples of Up price against BTC distance from the price to beat.`;
  return (
    <PairCard
      title="BTC vs strike → Up price"
      info="Each dot is one second: BTC distance from the price to beat in σ units (ln(S/K) ÷ σ√t) against the Up mid. Color = time left. The dashed curve is fair value for the current time left."
      legend={legend}
      takeaway={data?.takeaways.scatter ?? null}
      plotLabel={plotLabel}
    >
      {(w, h) => <ScatterPlot data={data} w={w} h={h} />}
    </PairCard>
  );
}

function ScatterPlot({ data, w, h }: { data: AnalysisPayload | null; w: number; h: number }) {
  const clipId = React.useId();
  const x0 = M.l;
  const x1 = w - M.r;
  const y0 = M.t;
  const y1 = h - M.b;
  const x = linear(Z_MIN, Z_MAX, x0, x1);
  const y = linear(0, 100, y1, y0);
  const scatter = data?.scatter;
  const { layers } = React.useMemo(
    () => scatterLayers(scatter ?? [], linear(Z_MIN, Z_MAX, M.l, w - M.r), linear(0, 100, h - M.b, M.t)),
    [scatter, w, h],
  );
  const fairD = linePath(
    (data?.fairCurve ?? []).filter((p) => p.z >= Z_MIN - 0.05 && p.z <= Z_MAX + 0.05).map((p) => [x(p.z), y(p.fair)] as const),
  );
  const cur = data?.current ?? null;
  const curIn = cur && Number.isFinite(cur.z) && Number.isFinite(cur.upMid);
  const cx = curIn ? x(Math.min(Z_MAX, Math.max(Z_MIN, cur!.z))) : 0;
  const cy = curIn ? y(Math.min(100, Math.max(0, cur!.upMid))) : 0;
  const offScale = curIn && (cur!.z < Z_MIN || cur!.z > Z_MAX);
  const nowRight = cx < x1 - 34;
  const empty = !data || data.scatter.length === 0;

  return (
    <svg width={w} height={h} aria-hidden className="block overflow-visible">
      <defs>
        <clipPath id={clipId}>
          <rect x={x0} y={y0 - 4} width={x1 - x0} height={y1 - y0 + 8} />
        </clipPath>
      </defs>
      {/* grid */}
      {[0, 50, 100].map((c) => (
        <g key={c}>
          <line x1={x0} x2={x1} y1={y(c)} y2={y(c)} stroke={c === 50 ? C.gridStrong : C.grid} strokeDasharray={c === 50 ? "2 3" : undefined} />
          <text x={x0 - 4} y={y(c)} textAnchor="end" dominantBaseline="middle" {...AXIS_TEXT}>
            {c}
          </text>
        </g>
      ))}
      {[-2, -1, 0, 1, 2].map((z) => (
        <g key={z}>
          <line x1={x(z)} x2={x(z)} y1={y0} y2={y1} stroke={z === 0 ? C.gridStrong : C.grid} strokeDasharray={z === 0 ? "2 3" : undefined} />
          <text x={x(z)} y={h - 3} textAnchor={z === -2 ? "start" : z === 2 ? "end" : "middle"} {...AXIS_TEXT}>
            {z === 0 ? "0σ" : `${z > 0 ? "+" : "−"}${Math.abs(z)}σ`}
          </text>
        </g>
      ))}
      <text x={x0 + 3} y={y0 + 8} {...AXIS_TEXT}>
        ¢ Up
      </text>

      <g clipPath={`url(#${clipId})`}>
        {layers.map((l) =>
          l.pts.length ? (
            <path key={l.bucket} d={dotsPath(l.pts)} stroke={l.color} strokeWidth={3.2} strokeLinecap="round" fill="none" strokeOpacity={0.85} />
          ) : null,
        )}
        {fairD && <path d={fairD} fill="none" stroke={C.fair} strokeWidth={1.6} strokeDasharray="5 3" />}
      </g>

      {curIn && (
        <g>
          <circle cx={cx} cy={cy} r={5.5} fill="#0A0D12" fillOpacity={0.4} stroke={C.ptb} strokeWidth={2} />
          <text
            x={nowRight ? cx + 9 : cx - 9}
            y={cy - 7 < y0 + 8 ? cy + 15 : cy - 7}
            textAnchor={nowRight ? "start" : "end"}
            fontSize={AXIS_FONT}
            fontWeight={600}
            fill={C.ptb}
            className="font-mono"
          >
            {offScale ? `now ${cur!.z > 0 ? "+" : "−"}${Math.abs(cur!.z).toFixed(1)}σ` : "now"}
          </text>
        </g>
      )}
      {empty && <PlotNote x={(x0 + x1) / 2} y={(y0 + y1) / 2}>{data ? "Not enough data yet" : "Loading…"}</PlotNote>}
    </svg>
  );
}
