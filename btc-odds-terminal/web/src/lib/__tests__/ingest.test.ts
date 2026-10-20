import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { TICK_CAP_MS, flush, ingest } from "../ingest";
import { useTerminal } from "../store";
import { MIN, resetStore, win } from "./helpers";

const T0 = 1_700_000_100_000 - (1_700_000_100_000 % (15 * MIN));
const cur = win(T0, "live");

beforeEach(() => {
  resetStore();
  useTerminal.setState({ current: cur, windows: { [cur.id]: cur } });
});

test("a newly discovered upcoming window becomes `next` (no rollover is sent for it)", () => {
  const nxt = win(T0 + 15 * MIN, "upcoming");
  ingest({ type: "window", data: nxt });
  flush();
  assert.equal(useTerminal.getState().next?.id, nxt.id);
  assert.equal(useTerminal.getState().current?.id, cur.id);
});

test("an earlier upcoming window replaces a later `next`; a later one does not", () => {
  const n2 = win(T0 + 30 * MIN, "upcoming");
  const n1 = win(T0 + 15 * MIN, "upcoming");
  ingest({ type: "window", data: n2 });
  ingest({ type: "window", data: n1 });
  flush();
  assert.equal(useTerminal.getState().next?.id, n1.id);
  ingest({ type: "window", data: n2 });
  flush();
  assert.equal(useTerminal.getState().next?.id, n1.id);
});

test("a stale `next` (already started) is replaced by a newly discovered upcoming window", () => {
  const stale = win(T0 - 15 * MIN, "upcoming");
  useTerminal.setState({ next: stale });
  const nxt = win(T0 + 15 * MIN, "upcoming");
  ingest({ type: "window", data: nxt });
  flush();
  assert.equal(useTerminal.getState().next?.id, nxt.id);
});

test("updates to the known next window still apply; upcoming windows before current end are ignored", () => {
  const nxt = win(T0 + 15 * MIN, "upcoming");
  useTerminal.setState({ next: nxt });
  ingest({ type: "window", data: { ...nxt, title: "renamed" } });
  ingest({ type: "window", data: win(T0, "upcoming", { id: "weird" }) });
  flush();
  assert.equal(useTerminal.getState().next?.title, "renamed");
});

test("when `next` goes live it becomes current and is no longer `next`", () => {
  const nxt = win(T0 + 15 * MIN, "upcoming");
  useTerminal.setState({ next: nxt });
  ingest({ type: "window", data: { ...nxt, status: "live" } });
  flush();
  const st = useTerminal.getState();
  assert.equal(st.current?.id, nxt.id);
  assert.equal(st.next, null);
});

test("raw ticks are trimmed to TICK_CAP_MS while bars keep the long horizon", () => {
  const now = T0 + 5 * MIN;
  const ticks = useTerminal.getState().series.ticks;
  const bars = useTerminal.getState().series.bars;
  // 2h of 1/s history, older than the tick cap
  for (let ts = now - 2 * 3600_000; ts < now; ts += 1000) {
    ticks.push({ ts, price: 1, sources: {} });
    bars.push({ t: ts, d: 1, o: 1, h: 1, l: 1, c: 1 });
  }
  ingest({ type: "btc", data: { ts: now, price: 1, sources: {} } });
  flush();
  const st = useTerminal.getState().series;
  assert.ok(st.ticks[0].ts >= now - TICK_CAP_MS, "ticks older than the cap are dropped");
  assert.ok(st.ticks.length <= TICK_CAP_MS / 1000 + 1);
  assert.equal(st.bars[0].t, now - 2 * 3600_000, "bars are not cut at the tick cap");
});
