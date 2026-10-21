"use client";
// Shared frame for the three Pair relationship cards: title + info, a measured SVG plot area, legend, takeaway.
// OWNER: tabs.
import * as React from "react";
import { InfoTip } from "@/components/ui/info-tip";
import { cn } from "@/lib/utils";

/**
 * Plot height in px (the bottom tabs strip is ~210px tall on desktop; the card must fit without scrolling).
 * 100 leaves room for a two-line takeaway.
 */
export const PLOT_H = 100;

/** Width of an element, tracked with ResizeObserver (0 until measured). */
export function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = React.useRef<T>(null);
  const [w, setW] = React.useState(0);
  React.useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(Math.floor(el.getBoundingClientRect().width));
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const cw = Math.floor(entries[0]?.contentRect.width ?? 0);
      setW((prev) => (prev === cw ? prev : cw));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export interface LegendItem {
  key: string;
  label: React.ReactNode;
  swatch: React.ReactNode;
}

/** Legend swatches */
export const DotSwatch = ({ color }: { color: string }) => (
  <span aria-hidden className="inline-block h-[7px] w-[7px] shrink-0 rounded-full" style={{ background: color }} />
);
export const LineSwatch = ({ color, dashed }: { color: string; dashed?: boolean }) => (
  <svg aria-hidden width="16" height="6" className="shrink-0">
    <line x1="0" y1="3" x2="16" y2="3" stroke={color} strokeWidth="2" strokeDasharray={dashed ? "4 3" : undefined} />
  </svg>
);
export const RingSwatch = ({ color }: { color: string }) => (
  <svg aria-hidden width="10" height="10" className="shrink-0">
    <circle cx="5" cy="5" r="3.5" fill="none" stroke={color} strokeWidth="1.6" />
  </svg>
);
export const BoxSwatch = ({ color }: { color: string }) => (
  <span aria-hidden className="inline-block h-[8px] w-[8px] shrink-0 rounded-[2px]" style={{ background: color }} />
);

export function PairCard({
  title,
  info,
  legend,
  takeaway,
  plotLabel,
  children,
  className,
}: {
  title: string;
  info: string;
  legend: LegendItem[];
  /** takeaway generated from the numbers (up to two lines; the full text is also in the plot's label) */
  takeaway: string | null;
  /** accessible summary of the plot */
  plotLabel: string;
  /** renders the plot for the measured width */
  children: (width: number, height: number) => React.ReactNode;
  className?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const titleId = React.useId();
  return (
    <section aria-labelledby={titleId} className={cn("flex min-w-0 flex-col rounded-card border border-border bg-inset px-2.5 pb-2 pt-2", className)}>
      <header className="flex items-center gap-1.5">
        <h3 id={titleId} className="label truncate">
          {title}
        </h3>
        <InfoTip label={info} side="top" />
      </header>
      <div ref={ref} role="img" aria-label={takeaway ? `${plotLabel} ${takeaway}` : plotLabel} className="relative mt-1 w-full" style={{ height: PLOT_H }}>
        {width > 0 && children(width, PLOT_H)}
      </div>
      <ul className="mt-1 flex min-h-[14px] flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11px] leading-[14px] text-muted" aria-label={`${title} legend`}>
        {legend.map((l) => (
          <li key={l.key} className="inline-flex items-center gap-1 whitespace-nowrap">
            {l.swatch}
            {l.label}
          </li>
        ))}
      </ul>
      {/* no aria-live: the numbers refresh every few seconds and three cards would queue announcements */}
      <p className="mt-1 line-clamp-2 min-h-[32px] text-[12px] leading-4 text-text-2" title={takeaway ?? undefined}>
        {takeaway ?? "Not enough data yet."}
      </p>
    </section>
  );
}

/** Centered message over an empty plot. */
export function PlotNote({ x, y, children }: { x: number; y: number; children: React.ReactNode }) {
  return (
    <text x={x} y={y} textAnchor="middle" dominantBaseline="middle" fontSize="11" fill="#A3ABB9" className="font-sans">
      {children}
    </text>
  );
}

/** SVG font size for axis numbers and in-plot labels (SPEC: 11px labels). */
export const AXIS_FONT = 11;
/** SVG font props for axis numbers (JetBrains Mono, labels color: 5.8:1 on the inset background). */
export const AXIS_TEXT = { fontSize: AXIS_FONT, fill: "#8A93A3", className: "font-mono" } as const;
