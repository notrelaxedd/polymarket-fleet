// Chart-local design tokens (mirrors SPEC "Tokens"). Canvas code cannot read Tailwind classes,
// so the values live here as plain strings.

export const C = {
  bg: "#0A0D12",
  surface: "#0E1218",
  surface2: "#151B24",
  border: "#1E2530",
  borderStrong: "#2A3442",
  text: "#E6E9EF",
  text2: "#C9CED8",
  muted: "#A3ABB9",
  label: "#8A93A3",
  up: "#34D399",
  down: "#F87171",
  odds: "#5B9CFF",
  oddsText: "#7FB2FF",
  oddsBand: "rgba(91, 156, 255, 0.16)",
  downOdds: "rgba(248, 113, 113, 0.62)",
  fair: "#C29BFF",
  ptb: "#F5B942",
  ptbFaded: "rgba(245, 185, 66, 0.45)",
  negEdge: "#F59E0B",
  grid: "rgba(30, 37, 48, 0.55)",
  btcLine: "#E6E9EF",
  /** window bands */
  bandCurrent: "rgba(255, 255, 255, 0.028)",
  bandPrevious: "rgba(0, 0, 0, 0.32)",
  boundary: "rgba(163, 171, 185, 0.38)",
  nowLine: "rgba(201, 206, 216, 0.55)",
  /** per-exchange BTC lines */
  exchange: {
    coinbase: "rgba(56, 189, 248, 0.6)",
    kraken: "rgba(232, 121, 249, 0.55)",
    bitstamp: "rgba(45, 212, 191, 0.55)",
  },
} as const;

export const MONO_FALLBACK = `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace`;

/**
 * Canvas text cannot use `var(--font-mono)`; resolve the next/font generated family name
 * (set by the app shell on <html>/<body>) and fall back to the plain family names.
 */
export function resolveMonoFont(el?: Element | null): string {
  if (typeof window === "undefined") return MONO_FALLBACK;
  const targets = [el, document.body, document.documentElement].filter(Boolean) as Element[];
  for (const t of targets) {
    const v = getComputedStyle(t).getPropertyValue("--font-mono").trim();
    if (v) return `${v}, ${MONO_FALLBACK}`;
  }
  return MONO_FALLBACK;
}

/** Shared canvas font string for primitives (updated once fonts resolve). */
export const fonts = { mono: MONO_FALLBACK };

export function monoFont(sizePx: number, weight = 500): string {
  return `${weight} ${sizePx}px ${fonts.mono}`;
}
