import * as React from "react";

/**
 * Hand-made round SVG badge stickers (NOT emoji). Each renders in a 24x24 viewBox.
 * `STICKERS` is the shared map so the chart layer can reuse the same artwork later.
 */
export type StickerId = "target" | "rocket" | "warning" | "check" | "cross" | "bull" | "bear" | "lightning";

export interface StickerDef {
  id: StickerId;
  label: string;
  /** badge fill */
  color: string;
  /** glyph markup (white strokes/fills) drawn on top of the badge circle */
  glyph: React.ReactNode;
}

const S = { fill: "none", stroke: "#fff", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" } as const;

export const STICKERS: Record<StickerId, StickerDef> = {
  target: {
    id: "target",
    label: "Target",
    color: "#DC4B4B",
    glyph: (
      <>
        <circle cx="12" cy="12" r="6.2" {...S} />
        <circle cx="12" cy="12" r="2.6" {...S} />
        <circle cx="12" cy="12" r="0.6" fill="#fff" stroke="#fff" strokeWidth="1" />
      </>
    ),
  },
  rocket: {
    id: "rocket",
    label: "Rocket",
    color: "#3F7FE0",
    glyph: (
      <>
        <path d="M12 4.2c2.7 1.8 4 4.6 4 8l-1.9 2.1H9.9L8 12.2c0-3.4 1.3-6.2 4-8z" {...S} strokeWidth="1.7" />
        <circle cx="12" cy="9.6" r="1.35" fill="#fff" />
        <path d="M8 12.4 5.9 14.9l3 .1M16 12.4l2.1 2.5-3 .1" {...S} strokeWidth="1.5" />
        <path d="M10.6 17.2 12 20l1.4-2.8" {...S} strokeWidth="1.5" />
      </>
    ),
  },
  warning: {
    id: "warning",
    label: "Warning",
    color: "#D9930D",
    glyph: (
      <>
        <path d="M12 5.6 19.2 18H4.8L12 5.6z" {...S} strokeWidth="1.8" />
        <path d="M12 10.2v3.6" {...S} />
        <circle cx="12" cy="16" r="0.4" fill="#fff" stroke="#fff" strokeWidth="1.2" />
      </>
    ),
  },
  check: {
    id: "check",
    label: "Check",
    color: "#22A06B",
    glyph: <path d="M7 12.6l3.3 3.3L17.2 8.8" {...S} strokeWidth="2.5" />,
  },
  cross: {
    id: "cross",
    label: "Cross",
    color: "#D04545",
    glyph: <path d="M8.2 8.2l7.6 7.6M15.8 8.2l-7.6 7.6" {...S} strokeWidth="2.5" />,
  },
  bull: {
    id: "bull",
    label: "Bull · up trend",
    color: "#1E9E6E",
    glyph: (
      <>
        <path d="M5.8 15.4l4.2-4.2 3 3 5-5.4" {...S} strokeWidth="2" />
        <path d="M14.4 8.2h3.9v3.9" {...S} strokeWidth="2" />
      </>
    ),
  },
  bear: {
    id: "bear",
    label: "Bear · down trend",
    color: "#D04545",
    glyph: (
      <>
        <path d="M5.8 8.6l4.2 4.2 3-3 5 5.4" {...S} strokeWidth="2" />
        <path d="M14.4 15.8h3.9v-3.9" {...S} strokeWidth="2" />
      </>
    ),
  },
  lightning: {
    id: "lightning",
    label: "Lightning",
    color: "#7B5BD6",
    glyph: <path d="M13.2 4.4 7.2 13h4.3l-.9 6.6 6.2-8.8h-4.4l.8-6.4z" fill="#fff" stroke="#fff" strokeWidth="0.8" strokeLinejoin="round" />,
  },
};

export const STICKER_IDS: StickerId[] = ["target", "rocket", "warning", "check", "cross", "bull", "bear", "lightning"];

export function Sticker({ id, size = 28, className }: { id: StickerId; size?: number; className?: string }) {
  const d = STICKERS[id];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={className} role="img" aria-label={d.label}>
      <circle cx="12" cy="12" r="11.2" fill={d.color} stroke="rgba(255,255,255,0.28)" strokeWidth="0.8" />
      {d.glyph}
    </svg>
  );
}
