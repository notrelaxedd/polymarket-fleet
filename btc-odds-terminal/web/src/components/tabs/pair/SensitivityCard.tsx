"use client";
// Card 3: Sensitivity — ¢ of Up per $10 of BTC over the current window: market (solid blue) vs fair (dashed
// purple), current values labeled at "now", the rest of the window shaded. OWNER: tabs.
import * as React from "react";
import type { AnalysisPayload, SensitivityPoint } from "@shared/protocol";
import { AXIS_FONT, AXIS_TEXT, BoxSwatch, LineSwatch, PairCard, PlotNote } from "./frame";
import { PAIR_COLORS as C, lastValue, linePath, linear, niceTicks, sensitivityDomain, sensitivityWindow, separateLabels } from "./geom";

const M = { l: 32, r: 8, t: 8, b: 16 };

const fmtHM = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", hour12: true });
/** "10:35" (ET, no period) for axis ticks */
const tickTime = (ms: number) => fmtHM.format(ms).replace(/\s?[AP]M$/, "");

const fmtSens = (v: number) => (Math.abs(v) >= 10 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(2));

export function SensitivityCard({ data }: { data: AnalysisPayload | null }) {
  const pts = data?.sensitivity ?? [];
  const m = lastValue(pts, "market");
  const f = lastValue(pts, "fair");
  return (
    <PairCard
      title="Sensitivity"
      info="Cents the Up price moves per $10 of BTC, through the current window. Market = measured from Up-mid changes vs BTC changes; fair = slope of the fair-value model. Sensitivity rises toward expiry when BTC sits near the price to beat."
      legend={[
        { key: "m", label: <span>Market{m ? <span className="num text-odds-text"> {fmtSens(m.v)}¢</span> : null}</span>, swatch: <LineSwatch color={C.odds} /> },
        { key: "f", label: <span>Fair{f ? <span className="num text-fair"> {fmtSens(f.v)}¢</span> : null}</span>, swatch: <LineSwatch color={C.fair} dashed /> },
        { key: "rest", label: "Rest of window", swatch: <BoxSwatch color="#1E2632" /> },
      ]}
      takeaway={data?.takeaways.sensitivity ?? null}
      plotLabel={
        m || f
          ? `Line chart of Up cents per 10 dollars of BTC this window. Now: market ${m ? fmtSens(m.v) : "n/a"}, fair ${f ? fmtSens(f.v) : "n/a"} cents.`
          : "Line chart of Up cents per 10 dollars of BTC this window. Not enough data yet."
      }
    >
      {(w, h) => <SensitivityPlot pts={pts} computedTs={data?.computedTs ?? null} loading={!data} w={w} h={h} />}
    </PairCard>
  );
}

function SensitivityPlot({
  pts,
  computedTs,
  loading,
  w,
  h,
}: {
  pts: SensitivityPoint[];
  computedTs: number | null;
  loading: boolean;
  w: number;
  h: number;
}) {
  const clipId = React.useId();
  const x0 = M.l;
  const x1 = w - M.r;
  const y0 = M.t;
  const y1 = h - M.b;
  const win = sensitivityWindow(pts);
  const [lo, hi] = sensitivityDomain(pts);
  const y = linear(lo, hi, y1, y0);
  const x = win ? linear(win.startTs, win.endTs, x0, x1) : linear(0, 1, x0, x1);
  const marketD = linePath(pts.map((p) => [x(p.ts), p.market == null ? null : y(p.market)] as const));
  const fairD = linePath(pts.map((p) => [x(p.ts), p.fair == null ? null : y(p.fair)] as const));
  const yTicks = niceTicks(lo, hi, 2).filter((t) => t >= lo - 1e-9 && t <= hi + 1e-9);

  const lastTs = pts.length ? pts[pts.length - 1].ts : null;
  const nowTs = win ? Math.min(win.endTs, Math.max(win.startTs, Math.max(computedTs ?? 0, lastTs ?? 0))) : null;
  const nowX = nowTs != null ? x(nowTs) : null;
  const m = lastValue(pts, "market");
  const f = lastValue(pts, "fair");
  const [ym, yf] = separateLabels(m ? y(m.v) : null, f ? y(f.v) : null, 12, y0 + 6, y1 - 4);
  // labels go into the shaded future area when there is room, else to the left of "now"
  const labelsRight = nowX != null && x1 - nowX > 40;
  const lx = nowX == null ? 0 : labelsRight ? nowX + 6 : nowX - 6;
  const anchor = labelsRight ? "start" : "end";

  return (
    <svg width={w} height={h} aria-hidden className="block overflow-visible">
      <defs>
        <clipPath id={clipId}>
          <rect x={x0} y={y0} width={Math.max(0, x1 - x0)} height={Math.max(0, y1 - y0)} />
        </clipPath>
      </defs>
      {nowX != null && x1 - nowX > 0.5 && <rect x={nowX} y={y0} width={x1 - nowX} height={y1 - y0} fill="#1E2632" fillOpacity={0.55} />}
      <text x={x0 + 3} y={y0 + 8} {...AXIS_TEXT}>
        ¢ / $10
      </text>
      {yTicks.map((t) => (
        <g key={t}>
          <line x1={x0} x2={x1} y1={y(t)} y2={y(t)} stroke={t === 0 ? C.gridStrong : C.grid} />
          <text x={x0 - 4} y={y(t)} textAnchor="end" dominantBaseline="middle" {...AXIS_TEXT}>
            {+t.toFixed(2)}
          </text>
        </g>
      ))}
      {win &&
        [0, 5, 10, 15].map((min) => {
          const ts = win.startTs + min * 60_000;
          return (
            <g key={min}>
              {min > 0 && min < 15 && <line x1={x(ts)} x2={x(ts)} y1={y0} y2={y1} stroke={C.grid} />}
              <text x={x(ts)} y={h - 3} textAnchor={min === 0 ? "start" : min === 15 ? "end" : "middle"} {...AXIS_TEXT}>
                {tickTime(ts)}
              </text>
            </g>
          );
        })}

      <g clipPath={`url(#${clipId})`}>
        {fairD && <path d={fairD} fill="none" stroke={C.fair} strokeWidth={1.6} strokeDasharray="5 3" strokeLinejoin="round" />}
        {marketD && <path d={marketD} fill="none" stroke={C.odds} strokeWidth={1.8} strokeLinejoin="round" />}
      </g>

      {nowX != null && (
        <g>
          <line x1={nowX} x2={nowX} y1={y0} y2={y1} stroke="#A3ABB9" strokeDasharray="1 3" />
          <text x={nowX + (labelsRight ? 4 : -4)} y={y1 - 3} textAnchor={anchor} fontSize={AXIS_FONT} fill="#A3ABB9" className="font-mono">
            NOW
          </text>
          {m && ym != null && <circle cx={x(m.ts)} cy={Math.min(y1, Math.max(y0, y(m.v)))} r={2.5} fill={C.odds} />}
          {f && yf != null && <circle cx={x(f.ts)} cy={Math.min(y1, Math.max(y0, y(f.v)))} r={2.5} fill={C.fair} />}
          {m && ym != null && (
            <text x={lx} y={ym} dominantBaseline="middle" textAnchor={anchor} fontSize={AXIS_FONT} fontWeight={600} fill={C.oddsText} className="font-mono">
              {fmtSens(m.v)}¢
            </text>
          )}
          {f && yf != null && (
            <text x={lx} y={yf} dominantBaseline="middle" textAnchor={anchor} fontSize={AXIS_FONT} fontWeight={600} fill={C.fair} className="font-mono">
              {fmtSens(f.v)}¢
            </text>
          )}
        </g>
      )}
      {!m && !f && <PlotNote x={(x0 + x1) / 2} y={(y0 + y1) / 2}>{loading ? "Loading…" : "Not enough data yet"}</PlotNote>}
    </svg>
  );
}
