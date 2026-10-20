import type { FairSnap, MarketWindow, OddsSnap, StateSnapshot } from "@shared/protocol";
import { DEFAULT_TICKET, REPLAY_IDLE, useTerminal } from "../store";

export const MIN = 60_000;

export function win(startTs: number, status: MarketWindow["status"], over: Partial<MarketWindow> = {}): MarketWindow {
  const id = `btc-updown-15m-${startTs / 1000}`;
  return {
    id,
    title: "Bitcoin Up or Down - 15 min",
    startTs,
    endTs: startTs + 15 * MIN,
    priceToBeat: null,
    upInstrument: `${id}:up`,
    downInstrument: `${id}:down`,
    status,
    result: null,
    settlePrice: null,
    ...over,
  };
}

export const odds = (windowId: string, ts = 0): OddsSnap => ({ ts, windowId, upBid: 55.5, upAsk: 56.5, downBid: 43.5, downAsk: 44.5, upMid: 56 });
export const fair = (windowId: string, ts = 0): FairSnap => ({ ts, windowId, fair: 58, sigma: 0.0001, tLeftSec: 300, edge: 2 });

export function snapshot(serverTime: number, over: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    serverTime,
    settings: { mode: "paper", volLookbackMin: 30, feeCentsPerContract: null, feeNote: "" },
    current: null,
    next: null,
    recent: [],
    lastTick: null,
    lastOdds: null,
    lastFair: null,
    feeds: [],
    ...over,
  };
}

/** Reset the data part of the singleton store between tests. */
export function resetStore(): void {
  useTerminal.setState({
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
    alerts: [],
    alertEvents: [],
    unseenAlertEvents: 0,
    analysis: {},
    replay: REPLAY_IDLE,
    liveConfirmedThisSession: false,
    liveSession: null,
    ticket: DEFAULT_TICKET,
    bracketDrafts: {},
    notices: [],
  });
}
