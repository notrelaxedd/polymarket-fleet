import * as React from "react";

type P = { size?: number; className?: string };
const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
});

export const TrendLineIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M5 18 19 6" />
    <circle cx="5" cy="18" r="1.8" fill="currentColor" />
    <circle cx="19" cy="6" r="1.8" fill="currentColor" />
  </svg>
);
export const RayIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M5 17 20 7" />
    <path d="M15.5 6.5 20 7l-1 4.4" />
    <circle cx="5" cy="17" r="1.8" fill="currentColor" />
  </svg>
);
export const HLineIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M3 12h18" />
    <circle cx="12" cy="12" r="1.8" fill="currentColor" />
  </svg>
);
export const VLineIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M12 3v18" />
    <circle cx="12" cy="12" r="1.8" fill="currentColor" />
  </svg>
);
export const RectIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <rect x="4.5" y="6.5" width="15" height="11" rx="1.2" />
  </svg>
);
export const FibIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M3 5h18M3 10h18M3 15h18M3 20h18" />
    <path d="M6 20 18 5" strokeDasharray="2 2.4" />
  </svg>
);
export const MeasureIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} className={className}>
    <path d="M12 3v18M8.5 6.5 12 3l3.5 3.5M8.5 17.5 12 21l3.5-3.5" />
    <path d="M5 8h3M5 12h3M5 16h3" />
  </svg>
);
