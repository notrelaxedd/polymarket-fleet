// Small presentation helpers shared by the HUD components.
import type { CSSProperties } from 'react';
import { statusOf } from './fleet';
import type { Boot, FleetNode } from './fleet';

const TONE = { ok: 'var(--ok)', hot: 'var(--hot)', idle: 'var(--ok)', off: 'var(--off)' } as const;

/** The node's status colour as the `--c` custom property. */
export const tone = (n: FleetNode) => ({ '--c': TONE[statusOf(n)] }) as CSSProperties;

/** "just now", "12 s ago", "4 min ago", "3 h ago", "2 d ago" or "never". */
export function fmtAgo(seconds: number): string {
  if (seconds < 0) return 'never';
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

export const BOOT_WORD: Record<Boot, string> = { flash: 'Flash drive', ssd: 'SSD', hdd: 'Hard disk', unknown: 'Unknown' };
