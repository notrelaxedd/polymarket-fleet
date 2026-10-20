// Steps 3-6 store/ingest scaffold: upsert semantics, snapshot extras, alert events, drawings undo/redo,
// and the replay guard (live market data skipped while a replay is active; serverNow follows the cursor).
import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import type { AlertEvent, Order, Position } from "@shared/protocol";
import { flush, ingest, takeLiveDirty } from "../ingest";
import { liveNow, serverNow, useTerminal } from "../store";
import { MIN, odds, resetStore, snapshot, win } from "./helpers";

const T0 = 1_700_000_100_000 - (1_700_000_100_000 % (15 * MIN));
const cur = win(T0, "live");

const order = (id: string, status: Order["status"], createdTs = T0): Order => ({
  id, clientOrderId: null, externalId: null, mode: "paper", windowId: cur.id, instrument: cur.upInstrument, side: "up",
  action: "buy", type: "marketable", limitCents: 60, maxSlippageCents: 2, qty: 10, notionalUsd: 6, filledQty: 0,
  avgFillCents: null, feesUsd: 0, status, reason: "manual", bracketId: null, positionId: null, error: null,
  createdTs, updatedTs: createdTs,
});
const pos = (id: string, status: Position["status"]): Position => ({
  id, mode: "paper", windowId: cur.id, instrument: cur.upInstrument, side: "up", qty: 10, avgCents: 58, costUsd: 5.8,
  realizedPnlUsd: 0, feesUsd: 0, status, markCents: 60, unrealizedPnlUsd: 0.2, bracketId: null, openedTs: T0,
  updatedTs: T0, closedTs: null,
});

beforeEach(() => {
  resetStore();
  useTerminal.setState({ current: cur, windows: { [cur.id]: cur } });
});

test("orders / positions upsert by id; fills dedupe newest first", () => {
  ingest({ type: "orders", data: [order("o1", "open")] });
  ingest({ type: "orders", data: [order("o1", "filled"), order("o2", "open", T0 + 1)] });
  ingest({ type: "positions", data: [pos("p1", "open")] });
  const f = { id: "f1", orderId: "o1", externalId: null, mode: "paper" as const, windowId: cur.id, instrument: "x", side: "up" as const, action: "buy" as const, priceCents: 58, qty: 10, feeUsd: null, slippageCents: 0, ts: T0 };
  ingest({ type: "fills", data: [f] });
  ingest({ type: "fills", data: [f, { ...f, id: "f2", ts: T0 + 5 }] });
  flush();
  const st = useTerminal.getState();
  assert.equal(st.orders.o1.status, "filled");
  assert.equal(Object.keys(st.orders).length, 2);
  assert.deepEqual(st.fills.map((x) => x.id), ["f2", "f1"]);
  assert.equal(st.positions.p1.status, "open");
});

test("snapshot extras replace trading state; alertFired bumps the badge and lastFiredTs", () => {
  ingest({ type: "snapshot", data: { ...snapshot(T0, { current: cur }), orders: [order("o9", "open")], alerts: [{ id: "a1", kind: "btc_cross_ptb", threshold: null, enabled: true, once: false, note: null, lastFiredTs: null, createdTs: T0, updatedTs: T0 }] } });
  flush();
  assert.deepEqual(Object.keys(useTerminal.getState().orders), ["o9"]);
  const ev: AlertEvent = { id: 1, alertId: "a1", kind: "btc_cross_ptb", ts: T0 + 10, windowId: cur.id, value: 1, message: "x" };
  ingest({ type: "alertFired", data: ev });
  flush();
  const st = useTerminal.getState();
  assert.equal(st.unseenAlertEvents, 1);
  assert.equal(st.alerts[0].lastFiredTs, T0 + 10);
  assert.equal(st.alertEvents[0].id, 1);
});

test("drawings: commit / undo / redo; server echo does not touch the stacks", () => {
  const d = (id: string) => ({ id, kind: "hline" as const, points: [{ time: T0, price: 67000, scale: "btc" as const }], color: "#fff", locked: false, label: null, createdTs: T0, updatedTs: T0 });
  const st = useTerminal.getState();
  st.commitDrawings([d("a")]);
  useTerminal.getState().commitDrawings([d("a"), d("b")]);
  assert.deepEqual(useTerminal.getState().undoDrawings()?.map((x) => x.id), ["a"]);
  assert.deepEqual(useTerminal.getState().redoDrawings()?.map((x) => x.id), ["a", "b"]);
  ingest({ type: "drawings", data: [d("z")] });
  flush();
  assert.deepEqual(useTerminal.getState().drawings.map((x) => x.id), ["z"]);
  assert.equal(useTerminal.getState().drawingUndo.length, 2);
});

test("replay: live market data is skipped and serverNow follows the cursor", () => {
  useTerminal.getState().setReplay({ active: true, cursorTs: T0 + 1234 });
  takeLiveDirty();
  ingest({ type: "odds", data: odds(cur.id, T0 + 5000) });
  ingest({ type: "orders", data: [order("o1", "open")] });
  flush();
  const st = useTerminal.getState();
  assert.equal(st.lastOdds, null);
  assert.equal(st.series.odds.length, 0);
  assert.ok(st.orders.o1);
  assert.equal(serverNow(), T0 + 1234);
  assert.notEqual(liveNow(), T0 + 1234);
  assert.equal(takeLiveDirty(), true);
  useTerminal.getState().setReplay({ active: false, cursorTs: null });
  ingest({ type: "odds", data: odds(cur.id, T0 + 6000) });
  flush();
  assert.equal(useTerminal.getState().lastOdds?.ts, T0 + 6000);
});

test("rollover clears the previous window's book", () => {
  ingest({ type: "book", data: { ts: T0, windowId: cur.id, instrument: cur.upInstrument, bids: [{ price: 50, size: 5 }], asks: [], state: null } });
  flush();
  assert.equal(useTerminal.getState().book?.windowId, cur.id);
  const nxt = win(T0 + 15 * MIN, "live");
  ingest({ type: "rollover", data: { current: nxt, next: null } });
  flush();
  assert.equal(useTerminal.getState().book, null);
});
