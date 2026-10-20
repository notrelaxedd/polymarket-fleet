// Unit tests for the chart position lines (pure model, bracket write queue, store -> lines). No DOM. Run from web/:
//   NODE_OPTIONS=--conditions=import ../backend/node_modules/.bin/tsx --test src/components/chart/positions/__tests__/*.test.ts

import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import type { Bracket, BracketInput, Fill, Position } from "@shared/protocol";
import { STALE_AFTER_MS } from "@shared/protocol";
import { DEFAULT_TICKET, REPLAY_IDLE, useTerminal } from "@/lib/store";
import { MIN, odds, resetStore, win } from "@/lib/__tests__/helpers";
import { BracketSync } from "../bracketSync";
import { BOOK_FRESH_MS, linesFromState, quoteBids, ticketPreview } from "../controller";
import {
  axisTagText,
  buildLines,
  defaultLeg,
  draftLegCents,
  entryMarkers,
  exitingBracket,
  fmtPnl,
  layoutLabels,
  legBounds,
  nudgeCents,
  positionLegBounds,
  sameBracket,
  savedBracketUpdates,
  snapCents,
  withLeg,
  type PositionLine,
} from "../model";

const T0 = 1_700_000_100_000 - (1_700_000_100_000 % (15 * MIN));
const cur = win(T0, "live");

const pos = (over: Partial<Position> = {}): Position => ({
  id: "p1",
  mode: "paper",
  windowId: cur.id,
  instrument: cur.upInstrument,
  side: "up",
  qty: 96.2,
  avgCents: 26,
  costUsd: 25.01,
  realizedPnlUsd: 0,
  feesUsd: 0,
  status: "open",
  markCents: 88,
  unrealizedPnlUsd: 59.64,
  bracketId: "b1",
  openedTs: T0 + 60_000,
  updatedTs: T0 + 60_000,
  closedTs: null,
  ...over,
});

const bracket = (over: Partial<Bracket> = {}): Bracket => ({
  id: "b1",
  positionId: "p1",
  windowId: cur.id,
  tpCents: 96,
  slCents: 70,
  btcStop: null,
  timeExitSec: 30,
  maxSlippageCents: 2,
  status: "active",
  triggerReason: null,
  triggeredTs: null,
  exitOrderId: null,
  createdTs: T0,
  updatedTs: T0,
  ...over,
});

const fill = (over: Partial<Fill> = {}): Fill => ({
  id: "f1",
  orderId: "o1",
  externalId: null,
  mode: "paper",
  windowId: cur.id,
  instrument: cur.upInstrument,
  side: "up",
  action: "buy",
  priceCents: 26,
  qty: 96.2,
  feeUsd: null,
  slippageCents: 0,
  ts: T0 + 60_000,
  ...over,
});

const text = (l: PositionLine | undefined) => l?.runs.map((r) => r.text).join("") ?? "";
const byKey = (ls: PositionLine[], k: string) => ls.find((l) => l.key === k);

const input = (over: Partial<Parameters<typeof buildLines>[0]> = {}): Parameters<typeof buildLines>[0] => ({
  windowId: cur.id,
  positions: [pos()],
  brackets: { b1: bracket() },
  drafts: {},
  fills: [fill()],
  ticket: null,
  bids: { up: 88, down: 12 },
  ...over,
});

// ---- labels ------------------------------------------------------------------------------------------

test("entry / TP / SL labels: live P&L on the entry, projected P&L at each leg", () => {
  const ls = buildLines(input());
  assert.deepEqual(
    ls.map((l) => l.key),
    ["p1:entry", "p1:tp", "p1:sl"],
  );
  assert.equal(text(byKey(ls, "p1:entry")), "UP 96.2 @ 26¢ +$59.64");
  // 96.2 * (96 - 26) / 100 = 67.34 ; 96.2 * (70 - 26) / 100 = 42.33
  assert.equal(text(byKey(ls, "p1:tp")), "TP 96¢ +$67.34");
  assert.equal(text(byKey(ls, "p1:sl")), "SL 70¢ +$42.33");
  assert.equal(byKey(ls, "p1:tp")?.cents, 96);
  assert.equal(byKey(ls, "p1:entry")?.cents, 26);
  assert.equal(byKey(ls, "p1:entry")?.runs[2].tone, "pos");
});

test("a losing SL reads negative; Down positions say DOWN and sit at Down prices", () => {
  const ls = buildLines(
    input({
      positions: [pos({ side: "down", instrument: cur.downInstrument, qty: 40, avgCents: 30, markCents: 28, unrealizedPnlUsd: -0.8 })],
      brackets: { b1: bracket({ tpCents: 45, slCents: 20 }) },
      fills: [fill({ side: "down", priceCents: 30, qty: 40 })],
      bids: { up: 72, down: 28 },
    }),
  );
  assert.equal(text(byKey(ls, "p1:entry")), "DOWN 40 @ 30¢ −$0.80");
  assert.equal(byKey(ls, "p1:entry")?.runs[0].tone, "side-down");
  assert.equal(text(byKey(ls, "p1:sl")), "SL 20¢ −$4.00");
  assert.equal(byKey(ls, "p1:sl")?.runs[2].tone, "neg");
  assert.equal(byKey(ls, "p1:tp")?.cents, 45);
});

test("only open positions of the chart's window are drawn; the bracket draft wins over the stored bracket", () => {
  const other = pos({ id: "p2", windowId: "other-window" });
  const closed = pos({ id: "p3", status: "closed", qty: 0 });
  const ls = buildLines(input({ positions: [pos(), other, closed], drafts: { p1: { tpCents: 91, slCents: null, btcStop: null, timeExitSec: 30 } } }));
  assert.deepEqual(
    ls.map((l) => l.key),
    ["p1:entry", "p1:tp"],
  );
  assert.equal(text(byKey(ls, "p1:tp")), "TP 91¢ +$62.53");
  assert.equal(buildLines(input({ windowId: null })).length, 0);
});

test("cancelled brackets draw nothing; the entry then offers +TP / +SL", () => {
  const ls = buildLines(input({ brackets: { b1: bracket({ status: "cancelled" }) } }));
  assert.deepEqual(
    ls.map((l) => l.key),
    ["p1:entry"],
  );
  assert.equal(ls[0].addTp, true);
  assert.equal(ls[0].addSl, true);
  // a bid at 99¢ leaves no room for a take profit
  const top = buildLines(input({ positions: [pos({ markCents: 99 })], brackets: {}, bids: { up: 99, down: 1 } }));
  assert.equal(top[0].addTp, false);
  assert.equal(top[0].addSl, true);
});

test("no fresh bid for the side: legs cannot move and +TP / +SL are not offered", () => {
  const ls = buildLines(input({ bids: { up: null, down: 12 } }));
  assert.equal(byKey(ls, "p1:tp")?.bounds, null);
  assert.equal(byKey(ls, "p1:sl")?.bounds, null);
  const bare = buildLines(input({ brackets: {}, bids: { up: null, down: 12 } }));
  assert.equal(bare[0].addTp, false);
  assert.equal(bare[0].addSl, false);
  // the server mark alone (possibly old) is not enough
  assert.equal(positionLegBounds("sl", { tpCents: null, slCents: null }, null, 50), null);
  // fresh bid and mark: the range keeps clear of both
  assert.deepEqual(positionLegBounds("tp", { tpCents: null, slCents: 70 }, 88, 90), { lo: 91, hi: 99 });
  assert.deepEqual(positionLegBounds("sl", { tpCents: 96, slCents: null }, 88, 85), { lo: 1, hi: 84 });
  assert.deepEqual(positionLegBounds("sl", { tpCents: 96, slCents: null }, 88, null), { lo: 1, hi: 87 });
});

test("a triggered bracket: no TP/SL lines, the entry says the exit is working, no +TP / +SL", () => {
  const trig = bracket({ status: "triggered", triggerReason: "tp", triggeredTs: T0 + 90_000, updatedTs: T0 + 90_000 });
  const ls = buildLines(input({ brackets: { b1: trig }, drafts: { p1: { tpCents: 97, slCents: 70, btcStop: null, timeExitSec: null } } }));
  assert.deepEqual(
    ls.map((l) => l.key),
    ["p1:entry"],
  );
  assert.equal(text(ls[0]), "UP 96.2 @ 26¢ +$59.64 TP triggered · exiting");
  assert.match(ls[0].aria, /TP triggered · exiting/);
  assert.equal(ls[0].addTp, false);
  assert.equal(ls[0].addSl, false);
  assert.equal(text(buildLines(input({ brackets: { b1: { ...trig, triggerReason: "sl" } } }))[0]).endsWith("SL triggered · exiting"), true);
  // the newest of active / triggered decides; old cancelled / done ones are ignored
  assert.equal(exitingBracket("p1", { b1: trig, b2: bracket({ id: "b2", updatedTs: T0 + 95_000 }) }), null);
  assert.equal(exitingBracket("p1", { b1: trig, b0: bracket({ id: "b0", status: "done", updatedTs: T0 + 99_000 }) }), trig);
});

test("axis tags mark Down lines", () => {
  assert.equal(axisTagText({ side: "up", cents: 47 }), "47¢");
  assert.equal(axisTagText({ side: "down", cents: 47 }), "D 47¢");
  assert.equal(axisTagText({ side: "down", cents: 55.55 }), "D 55.6¢");
});

test("PUT responses never overwrite a newer bracket state; removal spares brackets written later", () => {
  const triggered = bracket({ status: "triggered", updatedTs: T0 + 5_000 });
  // the WS 'triggered' arrived before the (older) PUT response
  assert.deepEqual(savedBracketUpdates({ b1: triggered }, "p1", bracket({ tpCents: 95, updatedTs: T0 + 4_000 }), T0 + 3_000, T0 + 6_000), []);
  const newer = bracket({ tpCents: 95, updatedTs: T0 + 6_000 });
  assert.deepEqual(savedBracketUpdates({ b1: bracket() }, "p1", newer, T0 + 3_000, T0 + 7_000), [newer]);
  assert.deepEqual(savedBracketUpdates({}, "p1", newer, T0, T0), [newer]);
  // removed: the old active one is retired, one created after the request went out is kept
  const later = bracket({ id: "b2", updatedTs: T0 + 9_000 });
  const out = savedBracketUpdates({ b1: bracket({ updatedTs: T0 + 1_000 }), b2: later }, "p1", null, T0 + 2_000, T0 + 10_000);
  assert.deepEqual(
    out.map((b) => [b.id, b.status, b.updatedTs]),
    [["b1", "cancelled", T0 + 10_000]],
  );
});

test("fmtPnl: sign, thousands, zero", () => {
  assert.equal(fmtPnl(59.644), "+$59.64");
  assert.equal(fmtPnl(-3.2), "−$3.20");
  assert.equal(fmtPnl(1234.5), "+$1,234.50");
  assert.equal(fmtPnl(0.001), "$0.00");
  assert.equal(fmtPnl(null), "—");
});

// ---- entry markers ------------------------------------------------------------------------------------

test("entry triangles: this position's buy fills, oldest first; falls back to the open time at the average", () => {
  const p = pos();
  const fills = [
    fill({ id: "f3", ts: T0 + 90_000, priceCents: 28 }),
    fill({ id: "sell", action: "sell", ts: T0 + 95_000 }),
    fill({ id: "down", side: "down", ts: T0 + 80_000 }),
    fill({ id: "old", ts: T0 - 60_000 }),
    fill({ id: "live", mode: "live", ts: T0 + 70_000 }),
    fill({ id: "f2", ts: T0 + 60_000, priceCents: 25 }),
  ];
  assert.deepEqual(entryMarkers(p, fills), [
    { ts: T0 + 60_000, cents: 25 },
    { ts: T0 + 90_000, cents: 28 },
  ]);
  assert.deepEqual(entryMarkers(p, []), [{ ts: p.openedTs, cents: 26 }]);
});

// ---- drag bounds ----------------------------------------------------------------------------------------

test("leg bounds: TP above the bid and the SL, SL below the bid and the TP (whole cents)", () => {
  assert.deepEqual(legBounds("tp", { tpCents: 96, slCents: 70, bid: 88, entry: null }), { lo: 89, hi: 99 });
  assert.deepEqual(legBounds("sl", { tpCents: 96, slCents: 70, bid: 88, entry: null }), { lo: 1, hi: 87 });
  // fractional bid: strictly above / below it
  assert.deepEqual(legBounds("tp", { tpCents: null, slCents: null, bid: 55.5, entry: null }), { lo: 56, hi: 99 });
  assert.deepEqual(legBounds("sl", { tpCents: null, slCents: null, bid: 55.5, entry: null }), { lo: 1, hi: 55 });
  // no quote: only the other leg constrains
  assert.deepEqual(legBounds("sl", { tpCents: 40, slCents: 20, bid: null, entry: null }), { lo: 1, hi: 39 });
  // preview: entry bounds too
  assert.deepEqual(legBounds("tp", { tpCents: null, slCents: null, bid: 50, entry: 60 }), { lo: 61, hi: 99 });
  assert.deepEqual(legBounds("sl", { tpCents: null, slCents: null, bid: 50, entry: 60 }), { lo: 1, hi: 49 });
  // empty range
  assert.equal(legBounds("tp", { tpCents: null, slCents: null, bid: 99, entry: null }), null);
  assert.equal(legBounds("sl", { tpCents: null, slCents: null, bid: 1, entry: null }), null);
});

test("snap / nudge: whole cents, clamped to the range", () => {
  const b = { lo: 89, hi: 99 };
  assert.equal(snapCents(93.4, b), 93);
  assert.equal(snapCents(93.6, b), 94);
  assert.equal(snapCents(70, b), 89);
  assert.equal(snapCents(120, b), 99);
  assert.equal(snapCents(null, b), null);
  assert.equal(snapCents(90, null), null);
  assert.equal(nudgeCents(96, 1, b), 97);
  assert.equal(nudgeCents(98, 5, b), 99);
  assert.equal(nudgeCents(90, -5, b), 89);
  assert.equal(nudgeCents(90, 1, null), null);
});

test("withLeg keeps the other legs and the slippage cap; sameBracket compares values", () => {
  const b: BracketInput = { tpCents: 96, slCents: 70, btcStop: 67_000, timeExitSec: 30, maxSlippageCents: 2 };
  const n = withLeg(b, "tp", 92);
  assert.deepEqual(n, { ...b, tpCents: 92 });
  assert.notEqual(n, b);
  assert.deepEqual(withLeg(b, "sl", null), { ...b, slCents: null });
  assert.deepEqual(withLeg(null, "sl", 40), { tpCents: null, slCents: 40, btcStop: null, timeExitSec: null, maxSlippageCents: null });
  assert.equal(sameBracket(b, { ...b }), true);
  assert.equal(sameBracket(b, n), false);
  assert.equal(sameBracket(null, { tpCents: null, slCents: null, btcStop: null, timeExitSec: null }), true);
});

test("defaultLeg: a quarter of the way to 99¢ / 1¢ (>= 3¢), inside the range", () => {
  assert.equal(defaultLeg("tp", 60, { lo: 61, hi: 99 }), 70);
  assert.equal(defaultLeg("sl", 60, { lo: 1, hi: 59 }), 45);
  assert.equal(defaultLeg("tp", 97, { lo: 98, hi: 99 }), 99);
  assert.equal(defaultLeg("sl", 3, { lo: 1, hi: 2 }), 1);
  assert.equal(defaultLeg("tp", 50, null), null);
});

test("draftLegCents parses ticket text, 1–99 only", () => {
  assert.equal(draftLegCents(true, "96"), 96);
  assert.equal(draftLegCents(true, " 55.5 "), 55.5);
  assert.equal(draftLegCents(false, "96"), null);
  assert.equal(draftLegCents(true, ""), null);
  assert.equal(draftLegCents(true, "."), null);
  assert.equal(draftLegCents(true, "0"), null);
  assert.equal(draftLegCents(true, "100"), null);
});

// ---- label layout --------------------------------------------------------------------------------------

test("labels right-align; overlapping ones slide left past the blocker", () => {
  const out = layoutLabels(
    [
      { key: "a", y: 100, w: 120 },
      { key: "b", y: 110, w: 80 }, // overlaps a vertically -> left of a
      { key: "c", y: 200, w: 90 }, // free
      { key: "d", y: 105, w: 50 }, // overlaps a and b -> left of b
    ],
    500,
    20,
  );
  const m = Object.fromEntries(out.map((p) => [p.key, p]));
  assert.deepEqual(m.a, { key: "a", left: 380, top: 90 });
  assert.deepEqual(m.b, { key: "b", left: 294, top: 100 });
  assert.deepEqual(m.c, { key: "c", left: 410, top: 190 });
  assert.deepEqual(m.d, { key: "d", left: 238, top: 95 });
  // not enough room: clamped at minLeft
  const tight = layoutLabels(
    [
      { key: "a", y: 50, w: 150 },
      { key: "b", y: 50, w: 150 },
    ],
    200,
    20,
  );
  assert.equal(tight[1].left, 4);
});

// ---- store -> lines --------------------------------------------------------------------------------------

beforeEach(() => {
  resetStore();
  useTerminal.setState({ current: cur, windows: { [cur.id]: cur } });
});

test("linesFromState: positions of the current window, nothing during a replay", () => {
  useTerminal.setState({ positions: { p1: pos() }, brackets: { b1: bracket() }, fills: [fill()] });
  assert.deepEqual(
    linesFromState(useTerminal.getState()).map((l) => l.key),
    ["p1:entry", "p1:tp", "p1:sl"],
  );
  useTerminal.setState({ replay: { ...REPLAY_IDLE, active: true, windowId: cur.id } });
  assert.equal(linesFromState(useTerminal.getState()).length, 0);
});

test("quoteBids: fresh odds first, then a fresh book; stale quotes count as none", () => {
  const now = T0 + 120_000;
  const st = () => useTerminal.getState();
  assert.deepEqual(quoteBids(st(), now), { up: null, down: null });
  useTerminal.setState({ lastOdds: odds(cur.id, now - 1_000) });
  assert.deepEqual(quoteBids(st(), now), { up: 55.5, down: 43.5 });
  useTerminal.setState({ lastOdds: odds(cur.id, now - STALE_AFTER_MS - 1) });
  assert.deepEqual(quoteBids(st(), now), { up: null, down: null });
  const book = {
    ts: now - 2_000,
    windowId: cur.id,
    instrument: cur.upInstrument,
    bids: [{ price: 54, size: 10 }],
    asks: [{ price: 57, size: 10 }],
    state: null,
  };
  useTerminal.setState({ book });
  // Down bid = 100 - Up ask
  assert.deepEqual(quoteBids(st(), now), { up: 54, down: 43 });
  useTerminal.setState({ book: { ...book, ts: now - BOOK_FRESH_MS - 1 } });
  assert.deepEqual(quoteBids(st(), now), { up: null, down: null });
  // an expired window's quote is not this window's
  useTerminal.setState({ book: { ...book, windowId: "old" }, lastOdds: odds("old", now) });
  assert.deepEqual(quoteBids(st(), now), { up: null, down: null });
  // lines: legs locked without a quote, movable with one
  useTerminal.setState({ positions: { p1: pos() }, brackets: { b1: bracket() }, fills: [fill()] });
  assert.equal(linesFromState(st(), now).find((l) => l.key === "p1:sl")?.bounds, null);
  useTerminal.setState({ lastOdds: odds(cur.id, now) });
  // SL below min(fresh bid 55.5, mark 88)
  assert.deepEqual(linesFromState(st(), now).find((l) => l.key === "p1:sl")?.bounds, { lo: 1, hi: 55 });
});

test("ticket preview: TP/SL drafts with the estimated entry (current ask) and projected P&L", () => {
  useTerminal.setState({ lastOdds: odds(cur.id), ticket: { ...DEFAULT_TICKET, bracketOn: false, tpCents: "70", slCents: "45" } });
  assert.equal(ticketPreview(useTerminal.getState()), null);
  useTerminal.setState({ ticket: { ...useTerminal.getState().ticket, bracketOn: true, type: "marketable", usd: "25" } });
  const t = ticketPreview(useTerminal.getState());
  assert.ok(t);
  // marketable, no depth: fills at the 56.5¢ ask; 25 / 0.565 = 44.2 contracts
  assert.ok(t.entryCents != null && Math.abs(t.entryCents - 56.5) < 0.05, `entry ${t.entryCents}`);
  assert.equal(t.qty, 44.2);
  assert.equal(t.bid, 55.5);
  const ls = linesFromState(useTerminal.getState());
  assert.deepEqual(
    ls.map((l) => l.key),
    ["draft:tp", "draft:sl"],
  );
  // 44.2 * (70 - 56.5) / 100 = 5.967 ; 44.2 * (45 - 56.5) / 100 = -5.083
  assert.equal(text(ls[0]), "TP 70¢ +$5.97 est");
  assert.equal(text(ls[1]), "SL 45¢ −$5.08 est");
  assert.equal(ls[0].draft, true);
  assert.deepEqual(ls[0].bounds, { lo: 57, hi: 99 });
  assert.deepEqual(ls[1].bounds, { lo: 1, hi: 55 });
  // a leg switched off disappears
  useTerminal.setState({ ticket: { ...useTerminal.getState().ticket, slOn: false } });
  assert.deepEqual(
    linesFromState(useTerminal.getState()).map((l) => l.key),
    ["draft:tp"],
  );
});

// ---- bracket write queue ---------------------------------------------------------------------------------

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}
const tick = () => new Promise((r) => setImmediate(r));

function harness() {
  const drafts: Record<string, BracketInput> = {};
  const server: Record<string, BracketInput | null> = {};
  const puts: { id: string; body: BracketInput | null; d: Deferred<Bracket | null> }[] = [];
  const timers = new Map<number, () => void>();
  let seq = 0;
  const events: string[] = [];
  const sync = new BracketSync({
    put: (id, body) => {
      const d = deferred<Bracket | null>();
      puts.push({ id, body, d });
      return d.promise;
    },
    getDraft: (id) => drafts[id],
    getServer: (id) => server[id] ?? null,
    clearDraft: (id, sent) => {
      if (drafts[id] === sent) {
        delete drafts[id];
        events.push(`clear:${id}`);
      }
    },
    onSaved: (id, saved) => events.push(`saved:${id}:${saved?.tpCents ?? "null"}`),
    onError: (id, err) => events.push(`error:${id}:${(err as Error).message}`),
    setTimer: (fn) => {
      const id = ++seq;
      timers.set(id, fn);
      return id;
    },
    clearTimer: (t) => {
      timers.delete(t as number);
    },
  });
  const fire = () => {
    const fns = [...timers.values()];
    timers.clear();
    for (const f of fns) f();
  };
  return { drafts, server, puts, timers, events, sync, fire };
}
const leg = (tp: number | null, sl: number | null = 70): BracketInput => ({ tpCents: tp, slCents: sl, btcStop: null, timeExitSec: null, maxSlippageCents: 2 });

test("bracket sync: drags debounce into one PUT of the latest draft; draft dropped after release", async () => {
  const h = harness();
  h.sync.hold("p1");
  for (const tp of [93, 94, 95]) {
    h.drafts.p1 = leg(tp);
    h.sync.schedule("p1");
  }
  assert.equal(h.puts.length, 0);
  assert.equal(h.timers.size, 1);
  h.fire();
  assert.equal(h.puts.length, 1);
  assert.equal(h.puts[0].body?.tpCents, 95);
  h.puts[0].d.resolve(bracket({ tpCents: 95 }));
  await tick();
  // still held: keep the draft
  assert.deepEqual(h.events, ["saved:p1:95"]);
  assert.ok(h.drafts.p1);
  h.sync.release("p1");
  assert.equal(h.puts.length, 1, "nothing new to send");
  assert.deepEqual(h.events, ["saved:p1:95", "clear:p1"]);
  assert.equal(h.drafts.p1, undefined);
  assert.equal(h.sync.busy("p1"), false);
});

test("bracket sync: never two PUTs in flight; the newest draft follows the first", async () => {
  const h = harness();
  h.drafts.p1 = leg(93);
  h.sync.flush("p1");
  h.drafts.p1 = leg(97);
  h.sync.flush("p1");
  h.drafts.p1 = leg(98);
  h.sync.flush("p1");
  assert.equal(h.puts.length, 1);
  h.puts[0].d.resolve(bracket({ tpCents: 93 }));
  await tick();
  assert.equal(h.puts.length, 2);
  assert.equal(h.puts[1].body?.tpCents, 98);
  assert.equal(h.drafts.p1?.tpCents, 98, "draft kept while the follow-up is in flight");
  h.puts[1].d.resolve(bracket({ tpCents: 98 }));
  await tick();
  assert.deepEqual(h.events, ["saved:p1:93", "saved:p1:98", "clear:p1"]);
});

test("bracket sync: a draft replaced by someone else is not cleared", async () => {
  const h = harness();
  h.drafts.p1 = leg(93);
  h.sync.flush("p1");
  const mine = h.drafts.p1;
  h.drafts.p1 = { ...mine }; // e.g. the Edit bracket dialog wrote its own draft meanwhile (no schedule)
  h.puts[0].d.resolve(bracket({ tpCents: 93 }));
  await tick();
  assert.ok(h.drafts.p1, "foreign draft survives");
});

test("bracket sync: all legs off sends null; errors report and drop the queue", async () => {
  const h = harness();
  h.server.p1 = leg(96);
  h.drafts.p1 = leg(null, null);
  h.sync.flush("p1");
  assert.equal(h.puts[0].body, null);
  h.puts[0].d.resolve(null);
  await tick();
  assert.deepEqual(h.events, ["saved:p1:null", "clear:p1"]);

  h.events.length = 0;
  h.drafts.p2 = leg(90);
  h.sync.flush("p2");
  h.drafts.p2 = leg(91);
  h.sync.flush("p2"); // queued behind the failing one
  h.puts[1].d.reject(new Error("risk_rejected"));
  await tick();
  assert.equal(h.puts.length, 2, "queued write dropped after a failure");
  assert.deepEqual(h.events, ["error:p2:risk_rejected"]);
});

test("bracket sync: forget cancels pending timers and ignores the in-flight result", async () => {
  const h = harness();
  h.drafts.p1 = leg(93);
  h.sync.schedule("p1");
  h.sync.forget("p1");
  assert.equal(h.timers.size, 0);
  h.fire();
  assert.equal(h.puts.length, 0);
  h.sync.flush("p1");
  h.sync.forget("p1");
  h.puts[0].d.resolve(bracket({ tpCents: 93 }));
  await tick();
  assert.deepEqual(h.events, []);
});

test("bracket sync: dispose sends pending writes now and lets in-flight ones report and drop their draft", async () => {
  const h = harness();
  // p1: a keyboard nudge waiting for its debounce
  h.drafts.p1 = leg(93);
  h.sync.schedule("p1");
  // p2: a released drag already in flight
  h.drafts.p2 = leg(80);
  h.sync.flush("p2");
  // p3: in flight and failing
  h.drafts.p3 = leg(81);
  h.sync.flush("p3");
  // p4: still held by a drag (the controller reverts / releases first; dispose must not leave it held)
  h.sync.hold("p4");
  h.drafts.p4 = leg(82);
  h.sync.schedule("p4");
  h.sync.dispose();
  assert.equal(h.timers.size, 0);
  assert.deepEqual(
    h.puts.map((p) => [p.id, p.body?.tpCents]),
    [
      ["p2", 80],
      ["p3", 81],
      ["p1", 93],
      ["p4", 82],
    ],
  );
  h.sync.schedule("p1"); // no new work after dispose
  assert.equal(h.timers.size, 0);
  h.puts[0].d.resolve(bracket({ tpCents: 80 }));
  h.puts[1].d.reject(new Error("boom"));
  h.puts[2].d.resolve(bracket({ tpCents: 93 }));
  h.puts[3].d.resolve(bracket({ tpCents: 82 }));
  await tick();
  assert.deepEqual(h.events.sort(), ["clear:p1", "clear:p2", "clear:p4", "error:p3:boom", "saved:p1:93", "saved:p2:80", "saved:p4:82"]);
  assert.equal(h.drafts.p1, undefined);
  assert.equal(h.drafts.p2, undefined);
  assert.equal(h.drafts.p4, undefined);
  assert.ok(h.drafts.p3, "the caller's onError drops a failed draft (the controller does)");
});

test("bracket sync: a draft equal to the server's bracket is not sent (Esc revert, nudge back)", async () => {
  const h = harness();
  h.server.p1 = leg(96);
  h.sync.hold("p1");
  h.drafts.p1 = leg(93);
  h.sync.schedule("p1"); // dragged...
  h.drafts.p1 = leg(96); // ...and Esc wrote the original back (a fresh object)
  h.sync.release("p1");
  assert.equal(h.puts.length, 0, "no PUT for a cancelled change");
  assert.deepEqual(h.events, ["clear:p1"]);
  assert.equal(h.drafts.p1, undefined);
  // all legs off with no server bracket: nothing to remove
  h.drafts.p2 = leg(null, null);
  h.sync.flush("p2");
  assert.equal(h.puts.length, 0);
  assert.equal(h.drafts.p2, undefined);
  // a change still goes out; reverting while it is in flight sends the original back (server moved on)
  h.events.length = 0;
  h.drafts.p1 = leg(93);
  h.sync.flush("p1");
  h.drafts.p1 = leg(96);
  h.sync.flush("p1");
  assert.equal(h.puts.length, 1);
  h.server.p1 = leg(93); // the store got the saved bracket
  h.puts[0].d.resolve(bracket({ tpCents: 93 }));
  await tick();
  assert.equal(h.puts.length, 2);
  assert.equal(h.puts[1].body?.tpCents, 96);
});
