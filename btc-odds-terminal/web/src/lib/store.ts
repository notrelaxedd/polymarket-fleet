"use client";

import { create } from "zustand";
import type {
  Alert,
  AlertEvent,
  AlertInput,
  AnalysisPayload,
  AnalysisScope,
  Bar,
  BookLevels,
  Bracket,
  BracketInput,
  BtcTick,
  Drawing,
  FairSnap,
  FeedStatus,
  Fill,
  LiveStatus,
  MarketWindow,
  Notice,
  OddsSnap,
  Order,
  OrderType,
  Position,
  RiskState,
  Settings,
  Side,
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

/** Order ticket draft. Shared by the ticket (panels/OrderTicket) and the chart's pre-entry TP/SL lines
 *  (chart/positions): dragging a draft line writes tpCents / slCents here. Strings are raw input text. */
export interface TicketDraft {
  side: Side;
  type: OrderType;
  usd: string;
  /** limit price text (cents); "" = use the current ask */
  limitCents: string;
  maxSlippageCents: string;
  bracketOn: boolean;
  tpOn: boolean;
  tpCents: string;
  slOn: boolean;
  slCents: string;
  btcStopOn: boolean;
  btcStop: string;
  timeExitOn: boolean;
  timeExitSec: string;
}

export const DEFAULT_TICKET: TicketDraft = {
  side: "up",
  type: "limit",
  usd: "25",
  limitCents: "",
  maxSlippageCents: "2",
  bracketOn: false,
  tpOn: true,
  tpCents: "",
  slOn: true,
  slCents: "",
  btcStopOn: false,
  btcStop: "",
  timeExitOn: false,
  timeExitSec: "30",
};

export type ReplaySpeed = 1 | 4 | 16;

/** Replay of a recorded window. While `active`, live market data (btc/odds/fair/window/rollover) is NOT
 *  applied to the store (ingest skips it) and `serverNow()` returns `cursorTs`; lib/replay.ts drives it and
 *  calls ws.resyncLive() on exit. Trading / alerts / risk messages keep flowing. */
export interface ReplayState {
  active: boolean;
  windowId: string | null;
  speed: ReplaySpeed;
  playing: boolean;
  cursorTs: number | null;
  startTs: number | null;
  endTs: number | null;
  loading: boolean;
  error: string | null;
}

export const REPLAY_IDLE: ReplayState = {
  active: false,
  windowId: null,
  speed: 1,
  playing: false,
  cursorTs: null,
  startTs: null,
  endTs: null,
  loading: false,
  error: null,
};

/** NewAlertDialog open state (opened from the tools rail "Add alert", the Alerts tab, or the chart). */
export interface NewAlertUi {
  open: boolean;
  /** prefill, e.g. { kind: "btc_above", threshold: 67250 } from a chart click */
  prefill: Partial<AlertInput> | null;
  /** editing an existing alert */
  editId: string | null;
}

const UNDO_CAP = 100;
const FILLS_CAP = 500;
const EVENTS_CAP = 200;
const NOTICES_CAP = 20;

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

  // ---- steps 3-6 ----
  /** current window's Up book depth (null until one arrives / after rollover) */
  book: BookLevels | null;
  orders: Record<string, Order>;
  /** newest first, capped */
  fills: Fill[];
  positions: Record<string, Position>;
  brackets: Record<string, Bracket>;
  risk: RiskState | null;
  live: LiveStatus | null;
  /** server-side drawings (source of truth after each save) */
  drawings: Drawing[];
  /** undo / redo stacks of whole drawing lists (newest last) */
  drawingUndo: Drawing[][];
  drawingRedo: Drawing[][];
  selectedDrawingId: string | null;
  drawingsLocked: boolean;
  alerts: Alert[];
  /** newest first, capped */
  alertEvents: AlertEvent[];
  /** alert events since the Alerts tab was last viewed (tab badge) */
  unseenAlertEvents: number;
  newAlert: NewAlertUi;
  /** bottom-tab sample filter (This window / Last 20 / All) */
  sampleScope: AnalysisScope;
  analysis: Partial<Record<AnalysisScope, AnalysisPayload>>;
  replay: ReplayState;
  /** the user confirmed live trading in this browser session (LiveConfirmDialog) */
  liveConfirmedThisSession: boolean;
  /** session id from POST /api/live/confirm, sent as PlaceOrderRequest.liveSession */
  liveSession: string | null;
  ticket: TicketDraft;
  /** in-progress bracket edits by position id (chart drag / Edit bracket), before the debounced PUT lands */
  bracketDrafts: Record<string, BracketInput>;
  /** toasts, newest last */
  notices: Notice[];

  // actions
  applySnapshot: (s: StateSnapshot) => void;
  setHistory: (h: Partial<Series>) => void;
  setTimeframe: (t: Timeframe) => void;
  setChartStyle: (s: ChartStyle) => void;
  toggleOverlay: (k: keyof Overlays) => void;
  setHover: (h: HoverInfo | null) => void;
  setActiveTool: (t: string) => void;

  // ---- steps 3-6 actions ----
  setBook: (b: BookLevels | null) => void;
  upsertOrders: (o: Order[]) => void;
  addFills: (f: Fill[]) => void;
  upsertPositions: (p: Position[]) => void;
  upsertBrackets: (b: Bracket[]) => void;
  setRisk: (r: RiskState) => void;
  setLive: (l: LiveStatus) => void;
  /** server echo / initial load: replaces the list, no undo entry */
  setDrawings: (d: Drawing[]) => void;
  /** a local edit: pushes the current list on the undo stack, clears redo */
  commitDrawings: (next: Drawing[]) => void;
  /** returns the restored list (caller persists it), or null when the stack is empty */
  undoDrawings: () => Drawing[] | null;
  redoDrawings: () => Drawing[] | null;
  selectDrawing: (id: string | null) => void;
  setDrawingsLocked: (locked: boolean) => void;
  setAlerts: (a: Alert[]) => void;
  addAlertEvent: (e: AlertEvent) => void;
  setAlertEvents: (e: AlertEvent[]) => void;
  markAlertsSeen: () => void;
  openNewAlert: (prefill?: Partial<AlertInput> | null, editId?: string | null) => void;
  closeNewAlert: () => void;
  setSampleScope: (s: AnalysisScope) => void;
  setAnalysis: (scope: AnalysisScope, p: AnalysisPayload) => void;
  setReplay: (patch: Partial<ReplayState>) => void;
  setLiveSession: (session: string | null) => void;
  setTicket: (patch: Partial<TicketDraft>) => void;
  resetTicket: () => void;
  setBracketDraft: (positionId: string, draft: BracketInput | null) => void;
  pushNotice: (n: Notice) => void;
  dismissNotice: (id: string) => void;
}

/** upsert by id; a stored record with a strictly newer `updatedTs` wins over a late, older echo (WS or REST) */
const byId = <T extends { id: string; updatedTs?: number }>(cur: Record<string, T>, items: T[]): Record<string, T> => {
  if (!items.length) return cur;
  const out = { ...cur };
  for (const x of items) {
    const old = out[x.id];
    if (old && old.updatedTs != null && x.updatedTs != null && old.updatedTs > x.updatedTs) continue;
    out[x.id] = x;
  }
  return out;
};

/** merge newest-first lists by id (incoming wins), keep newest first by `ts`, cap */
function mergeNewest<T extends { id: string | number; ts: number }>(cur: T[], incoming: T[], cap: number): T[] {
  if (!incoming.length) return cur;
  const seen = new Map<string | number, T>();
  for (const x of cur) seen.set(x.id, x);
  for (const x of incoming) seen.set(x.id, x);
  return [...seen.values()].sort((a, b) => b.ts - a.ts).slice(0, cap);
}

/** Store fields filled from the optional step 3-6 parts of a StateSnapshot. */
function snapshotExtras(s: StateSnapshot, st: TerminalState): Partial<TerminalState> {
  const out: Partial<TerminalState> = {};
  if (s.book !== undefined) out.book = s.book;
  if (s.orders) out.orders = byId({}, s.orders);
  if (s.fills) out.fills = mergeNewest([], s.fills, FILLS_CAP);
  if (s.positions) out.positions = byId({}, s.positions);
  if (s.brackets) out.brackets = byId({}, s.brackets);
  if (s.risk) out.risk = s.risk;
  if (s.live) out.live = s.live;
  if (s.alerts) out.alerts = s.alerts;
  if (s.alertEvents) out.alertEvents = mergeNewest(st.alertEvents, s.alertEvents, EVENTS_CAP);
  if (s.drawings) out.drawings = s.drawings;
  return out;
}

export const useTerminal = create<TerminalState>()((set, get) => ({
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

  book: null,
  orders: {},
  fills: [],
  positions: {},
  brackets: {},
  risk: null,
  live: null,
  drawings: [],
  drawingUndo: [],
  drawingRedo: [],
  selectedDrawingId: null,
  drawingsLocked: false,
  alerts: [],
  alertEvents: [],
  unseenAlertEvents: 0,
  newAlert: { open: false, prefill: null, editId: null },
  sampleScope: "last20",
  analysis: {},
  replay: REPLAY_IDLE,
  liveConfirmedThisSession: false,
  liveSession: null,
  ticket: DEFAULT_TICKET,
  bracketDrafts: {},
  notices: [],

  applySnapshot: (s) =>
    set((st) => {
      const extras = snapshotExtras(s, st);
      // during a replay only settings + trading/alert state apply; market data stays on the replay
      if (st.replay.active) return { settings: s.settings, serverOffsetMs: s.serverTime - Date.now(), ...extras };
      const windows = { ...st.windows };
      for (const w of [s.current, s.next, ...s.recent]) if (w) windows[w.id] = w;
      return {
        ...extras,
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

  setBook: (book) => set({ book }),
  upsertOrders: (o) => set((st) => ({ orders: byId(st.orders, o) })),
  addFills: (f) => set((st) => ({ fills: mergeNewest(st.fills, f, FILLS_CAP) })),
  upsertPositions: (p) => set((st) => ({ positions: byId(st.positions, p) })),
  upsertBrackets: (b) => set((st) => ({ brackets: byId(st.brackets, b) })),
  setRisk: (risk) => set({ risk }),
  setLive: (live) => set({ live }),
  setDrawings: (drawings) =>
    set((st) => ({
      drawings,
      selectedDrawingId: st.selectedDrawingId && drawings.some((d) => d.id === st.selectedDrawingId) ? st.selectedDrawingId : null,
    })),
  commitDrawings: (next) =>
    set((st) => ({ drawings: next, drawingUndo: [...st.drawingUndo, st.drawings].slice(-UNDO_CAP), drawingRedo: [] })),
  undoDrawings: () => {
    const st = get();
    const prev = st.drawingUndo[st.drawingUndo.length - 1];
    if (!prev) return null;
    set({ drawings: prev, drawingUndo: st.drawingUndo.slice(0, -1), drawingRedo: [...st.drawingRedo, st.drawings].slice(-UNDO_CAP) });
    return prev;
  },
  redoDrawings: () => {
    const st = get();
    const next = st.drawingRedo[st.drawingRedo.length - 1];
    if (!next) return null;
    set({ drawings: next, drawingRedo: st.drawingRedo.slice(0, -1), drawingUndo: [...st.drawingUndo, st.drawings].slice(-UNDO_CAP) });
    return next;
  },
  selectDrawing: (selectedDrawingId) => set({ selectedDrawingId }),
  setDrawingsLocked: (drawingsLocked) => set({ drawingsLocked }),
  setAlerts: (alerts) => set({ alerts }),
  addAlertEvent: (e) =>
    set((st) => ({
      alertEvents: mergeNewest(st.alertEvents, [e], EVENTS_CAP),
      unseenAlertEvents: st.unseenAlertEvents + 1,
      alerts: st.alerts.map((a) => (a.id === e.alertId ? { ...a, lastFiredTs: Math.max(a.lastFiredTs ?? 0, e.ts) } : a)),
    })),
  setAlertEvents: (e) => set({ alertEvents: mergeNewest([], e, EVENTS_CAP) }),
  markAlertsSeen: () => set({ unseenAlertEvents: 0 }),
  openNewAlert: (prefill = null, editId = null) => set({ newAlert: { open: true, prefill, editId } }),
  closeNewAlert: () => set({ newAlert: { open: false, prefill: null, editId: null } }),
  setSampleScope: (sampleScope) => set({ sampleScope }),
  setAnalysis: (scope, p) => set((st) => ({ analysis: { ...st.analysis, [scope]: p } })),
  setReplay: (patch) => set((st) => ({ replay: { ...st.replay, ...patch } })),
  setLiveSession: (liveSession) => set({ liveSession, liveConfirmedThisSession: liveSession != null }),
  setTicket: (patch) => set((st) => ({ ticket: { ...st.ticket, ...patch } })),
  resetTicket: () => set({ ticket: DEFAULT_TICKET }),
  setBracketDraft: (positionId, draft) =>
    set((st) => {
      const bracketDrafts = { ...st.bracketDrafts };
      if (draft) bracketDrafts[positionId] = draft;
      else delete bracketDrafts[positionId];
      return { bracketDrafts };
    }),
  pushNotice: (n) => set((st) => ({ notices: [...st.notices.filter((x) => x.id !== n.id), n].slice(-NOTICES_CAP) })),
  dismissNotice: (id) => set((st) => ({ notices: st.notices.filter((n) => n.id !== id) })),
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

/** Server-corrected wall clock in ms (ignores replay). Use for feed ages, history ranges, REST timing. */
export const liveNow = () => Date.now() + useTerminal.getState().serverOffsetMs;

/**
 * Display clock in ms: the replay cursor while a replay is active, else liveNow(). The chart (now line,
 * framing), countdown and time-left read this, so a replay moves them without special cases.
 */
export const serverNow = () => {
  const st = useTerminal.getState();
  return st.replay.active && st.replay.cursorTs != null ? st.replay.cursorTs : Date.now() + st.serverOffsetMs;
};

/** Open positions (any window), newest first. */
export const selectOpenPositions = (s: Pick<TerminalState, "positions">): Position[] =>
  Object.values(s.positions)
    .filter((p) => p.status === "open")
    .sort((a, b) => b.openedTs - a.openedTs);

/** Orders newest first. */
export const selectOrders = (s: Pick<TerminalState, "orders">): Order[] =>
  Object.values(s.orders).sort((a, b) => b.createdTs - a.createdTs);

/** Active bracket of a position (bracket draft wins while dragging). */
export const selectBracketFor = (s: Pick<TerminalState, "brackets" | "bracketDrafts">, positionId: string): BracketInput | null => {
  const d = s.bracketDrafts[positionId];
  if (d) return d;
  for (const b of Object.values(s.brackets)) if (b.positionId === positionId && b.status === "active") return b;
  return null;
};
