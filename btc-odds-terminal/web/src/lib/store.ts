"use client";

import { create } from "zustand";
import type {
  Bar,
  BtcTick,
  FairSnap,
  FeedStatus,
  MarketWindow,
  OddsSnap,
  Settings,
  StateSnapshot,
} from "@shared/protocol";

export type Timeframe = 1 | 5 | 15 | 60 | 300; // seconds
export type ChartStyle = "candles" | "line";

export interface Overlays {
  upOdds: boolean;
  downOdds: boolean;
  fair: boolean;
  ptb: boolean;
  windows: boolean;
  edgePane: boolean;
  perExchange: boolean;
}

/** Values under the crosshair (null fields = latest values are shown instead). */
export interface HoverInfo {
  time: number; // ms
  o: number;
  h: number;
  l: number;
  c: number;
  upMid: number | null;
  upBid: number | null;
  upAsk: number | null;
  fair: number | null;
  edge: number | null;
  ptb: number | null;
  tLeftSec: number | null;
}

/** Live history kept in memory. Arrays are append-only, ascending by time; the chart reads them
 *  directly and is notified through `version` bumps (batched per animation frame by the WS client). */
export interface Series {
  bars: Bar[]; // 1s live bars + seeded history
  ticks: BtcTick[]; // raw proxy ticks for the last few hours (per-exchange lines)
  odds: OddsSnap[];
  fair: FairSnap[];
}

export interface TerminalState {
  connected: boolean;
  serverOffsetMs: number; // serverTime - Date.now()
  settings: Settings | null;
  current: MarketWindow | null;
  next: MarketWindow | null;
  windows: Record<string, MarketWindow>; // every window seen, by id
  recent: MarketWindow[]; // finished, newest first
  lastTick: BtcTick | null;
  lastOdds: OddsSnap | null;
  lastFair: FairSnap | null;
  feeds: FeedStatus[];
  series: Series;
  /** bumped whenever `series` changes (once per animation frame at most) */
  version: number;

  // UI
  timeframe: Timeframe;
  chartStyle: ChartStyle;
  overlays: Overlays;
  hover: HoverInfo | null;
  activeTool: string;

  // actions
  applySnapshot: (s: StateSnapshot) => void;
  setHistory: (h: Partial<Series>) => void;
  setTimeframe: (t: Timeframe) => void;
  setChartStyle: (s: ChartStyle) => void;
  toggleOverlay: (k: keyof Overlays) => void;
  setHover: (h: HoverInfo | null) => void;
  setActiveTool: (t: string) => void;
}

export const useTerminal = create<TerminalState>()((set) => ({
  connected: false,
  serverOffsetMs: 0,
  settings: null,
  current: null,
  next: null,
  windows: {},
  recent: [],
  lastTick: null,
  lastOdds: null,
  lastFair: null,
  feeds: [],
  series: { bars: [], ticks: [], odds: [], fair: [] },
  version: 0,

  timeframe: 15,
  chartStyle: "candles",
  overlays: {
    upOdds: true,
    downOdds: false,
    fair: true,
    ptb: true,
    windows: true,
    edgePane: true,
    perExchange: false,
  },
  hover: null,
  activeTool: "cursor",

  applySnapshot: (s) =>
    set((st) => {
      const windows = { ...st.windows };
      for (const w of [s.current, s.next, ...s.recent]) if (w) windows[w.id] = w;
      return {
        serverOffsetMs: s.serverTime - Date.now(),
        settings: s.settings,
        current: s.current,
        next: s.next,
        recent: s.recent,
        windows,
        lastTick: s.lastTick,
        lastOdds: s.lastOdds,
        lastFair: s.lastFair,
        feeds: s.feeds,
      };
    }),
  setHistory: (h) => set((st) => ({ series: { ...st.series, ...h }, version: st.version + 1 })),
  setTimeframe: (timeframe) => set({ timeframe }),
  setChartStyle: (chartStyle) => set({ chartStyle }),
  toggleOverlay: (k) => set((st) => ({ overlays: { ...st.overlays, [k]: !st.overlays[k] } })),
  setHover: (hover) => set({ hover }),
  setActiveTool: (activeTool) => set({ activeTool }),
}));

/**
 * `lastOdds` only when it belongs to the current window. Right after a rollover (or in a snapshot taken
 * then) the backend may still hold the expired window's quote; never show it against the new window.
 */
export const selectOdds = (s: Pick<TerminalState, "lastOdds" | "current">): OddsSnap | null =>
  s.lastOdds && s.current && s.lastOdds.windowId === s.current.id ? s.lastOdds : null;

/** `lastFair` only when it belongs to the current window (see selectOdds). */
export const selectFair = (s: Pick<TerminalState, "lastFair" | "current">): FairSnap | null =>
  s.lastFair && s.current && s.lastFair.windowId === s.current.id ? s.lastFair : null;

/** Server-corrected "now" in ms. */
export const serverNow = () => Date.now() + useTerminal.getState().serverOffsetMs;
