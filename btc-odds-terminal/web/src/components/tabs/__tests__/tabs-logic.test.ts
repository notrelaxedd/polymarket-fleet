// Bottom-tab logic: alert condition text / form validation / crossing hints, order slippage + fee cells,
// sample-filter window sets, per-window P&L and max edge, notification delivery plan.
// Run: tsx --test src/components/tabs/__tests__/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import type { Fill, MarketWindow, Order, Position } from "@shared/protocol";
import {
  DEFAULT_ALERT_FORM,
  alertCondition,
  crossingHint,
  formFromInput,
  formatThreshold,
  liveValueFor,
  alertEditPatch,
  parseThreshold,
  validateAlertForm,
} from "../alert-form";
import {
  SNAPSHOT_POSITIONS_LIMIT,
  SNAPSHOT_POSITIONS_MS,
  earliestTs,
  feeCell,
  fillSlippage,
  fillsByOrder,
  fillsMissing,
  maxEdgeByWindow,
  mergeById,
  mergeWindows,
  pageCovers,
  pnlByWindow,
  positionsCoverFrom,
  qtyText,
  scopeAnchor,
  slipVsLimit,
  summarizeOrders,
  windowsInScope,
} from "../table-math";
import { etDayPrefix } from "../table-ui";
import { SOUND_BLOCKED_HINT, alertToastMessage, deliveryPlan, installAudioUnlock, playAlertSound } from "../../../lib/notify";

// ---- alerts ----

test("alert condition text per kind", () => {
  assert.equal(alertCondition({ kind: "btc_above", threshold: 67250 }), "BTC above $67,250.00");
  assert.equal(alertCondition({ kind: "up_below", threshold: 40 }), "Up below 40¢");
  assert.equal(alertCondition({ kind: "down_above", threshold: 62.5 }), "Down above 62.5¢");
  assert.equal(alertCondition({ kind: "edge_above", threshold: 3 }), "Edge above +3¢");
  assert.equal(alertCondition({ kind: "edge_below", threshold: -2.5 }), "Edge below −2.5¢");
  assert.equal(alertCondition({ kind: "btc_cross_ptb", threshold: null }), "BTC crosses price to beat");
  assert.equal(formatThreshold("btc_below", null), "—");
});

test("parseThreshold accepts $, commas, ¢ and a unicode minus", () => {
  assert.equal(parseThreshold("$67,250.5"), 67250.5);
  assert.equal(parseThreshold(" 60¢ "), 60);
  assert.equal(parseThreshold("−3"), -3);
  assert.equal(parseThreshold(".5"), 0.5);
  assert.equal(parseThreshold(""), null);
  assert.equal(parseThreshold("abc"), null);
  assert.equal(parseThreshold("1e5"), null);
});

test("validateAlertForm: ranges, rounding, note, cross-PTB has no threshold", () => {
  const ok = validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "btc_above", threshold: "67,250.129", note: "  zone " });
  assert.deepEqual(ok, { ok: true, input: { kind: "btc_above", threshold: 67250.13, enabled: true, once: false, note: "zone" } });
  const up = validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "up_above", threshold: "60.04" });
  assert.equal(up.ok && up.input.threshold, 60);
  assert.equal(validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "up_above", threshold: "100" }).ok, false);
  assert.equal(validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "up_below", threshold: "0" }).ok, false);
  const edge = validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "edge_below", threshold: "-4" });
  assert.equal(edge.ok && edge.input.threshold, -4);
  assert.equal(validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "edge_above", threshold: "120" }).ok, false);
  const bad = validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "btc_below", threshold: "" });
  assert.equal(bad.ok, false);
  assert.equal(!bad.ok && bad.field, "threshold");
  assert.equal(validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "btc_below", threshold: "-1" }).ok, false);
  const cross = validateAlertForm({ ...DEFAULT_ALERT_FORM, kind: "btc_cross_ptb", threshold: "garbage", once: true, note: "" });
  assert.deepEqual(cross, { ok: true, input: { kind: "btc_cross_ptb", threshold: null, enabled: true, once: true, note: null } });
  const longNote = validateAlertForm({ ...DEFAULT_ALERT_FORM, threshold: "1", note: "x".repeat(200) });
  assert.equal(!longNote.ok && longNote.field, "note");
});

test("formFromInput: prefill over defaults / an edited alert", () => {
  assert.deepEqual(formFromInput({ kind: "btc_above", threshold: 67250 }), { ...DEFAULT_ALERT_FORM, kind: "btc_above", threshold: "67250" });
  const base = formFromInput({ kind: "up_below", threshold: 40.5, enabled: false, once: true, note: "n" });
  assert.deepEqual(base, { kind: "up_below", threshold: "40.5", enabled: false, once: true, note: "n" });
  assert.deepEqual(formFromInput(null, base), base);
  assert.equal(formFromInput({ kind: "btc_cross_ptb", threshold: 5 }).threshold, "");
});

test("live value per kind + crossing hints", () => {
  const v = { btc: 67100, upMid: 62, edge: -1.5, ptb: 67000 };
  assert.equal(liveValueFor("btc_below", v), 67100);
  assert.equal(liveValueFor("up_above", v), 62);
  assert.equal(liveValueFor("down_above", v), 38);
  assert.equal(liveValueFor("edge_below", v), -1.5);
  assert.equal(liveValueFor("btc_cross_ptb", v), 100);
  assert.equal(liveValueFor("down_below", { ...v, upMid: null }), null);
  assert.match(crossingHint("btc_above", 67000, 67100) ?? "", /Already above/);
  assert.match(crossingHint("up_below", 70, 62) ?? "", /Already below/);
  assert.equal(crossingHint("btc_above", 67200, 67100), null);
  assert.equal(crossingHint("btc_cross_ptb", null, 100), null);
});

// ---- orders & fills ----

const order = (o: Partial<Order>): Order => ({
  id: "o1",
  clientOrderId: null,
  externalId: null,
  mode: "paper",
  windowId: "w1",
  instrument: "up",
  side: "up",
  action: "buy",
  type: "limit",
  limitCents: 30,
  maxSlippageCents: 2,
  qty: 100,
  notionalUsd: 30,
  filledQty: 100,
  avgFillCents: 29.5,
  feesUsd: 0.5,
  status: "filled",
  reason: "manual",
  bracketId: null,
  positionId: null,
  error: null,
  createdTs: 1,
  updatedTs: 1,
  ...o,
});
const fill = (f: Partial<Fill>): Fill => ({
  id: "f1",
  orderId: "o1",
  externalId: null,
  mode: "paper",
  windowId: "w1",
  instrument: "up",
  side: "up",
  action: "buy",
  priceCents: 29,
  qty: 50,
  feeUsd: 0.25,
  slippageCents: 0,
  ts: 10,
  ...f,
});

test("slippage vs limit is positive when worse, per action", () => {
  assert.equal(slipVsLimit(order({})), -0.5); // bought below the limit
  assert.equal(slipVsLimit(order({ action: "sell", limitCents: 70, avgFillCents: 69 })), 1); // sold below the limit
  assert.equal(slipVsLimit(order({ filledQty: 0 })), null);
  assert.equal(slipVsLimit(order({ limitCents: null })), null);
});

test("fill slippage is quantity weighted; unknown fees show [FEE]", () => {
  assert.equal(fillSlippage([fill({ qty: 10, slippageCents: 1 }), fill({ qty: 30, slippageCents: 3 })]), 2.5);
  assert.equal(fillSlippage([fill({ slippageCents: null })]), null);
  assert.deepEqual(feeCell(order({}), [fill({ qty: 100 })]), { usd: 0.5, unknown: false, missing: false });
  assert.deepEqual(feeCell(order({}), [fill({}), fill({ id: "f2", feeUsd: null })]), { usd: null, unknown: true, missing: false });
});

test("an order whose fills are not loaded shows no fee (never $0.00) and counts as missing", () => {
  // the backend stores an unknown fee as feesUsd 0; with no fills loaded nothing tells it apart from a real 0
  assert.deepEqual(feeCell(order({ feesUsd: 0 }), []), { usd: null, unknown: false, missing: true });
  // only some fills loaded (store cap / WS gap)
  assert.deepEqual(feeCell(order({ filledQty: 100 }), [fill({ qty: 60 })]), { usd: null, unknown: false, missing: true });
  // float sums of 0.1 steps still count as complete
  assert.equal(fillsMissing({ filledQty: 0.3 }, [fill({ qty: 0.1 }), fill({ id: "f2", qty: 0.2 })]), false);
  // unfilled orders need no fills
  assert.deepEqual(feeCell(order({ filledQty: 0, feesUsd: 0 }), []), { usd: 0, unknown: false, missing: false });
  const s = summarizeOrders([order({ id: "a" }), order({ id: "b", filledQty: 0 })], new Map());
  assert.equal(s.fillsMissing, 1);
});

test("mergeById: the store wins unless the fetched copy is newer; pageCovers", () => {
  const storeO = order({ id: "x", status: "open", updatedTs: 5 });
  const merged = mergeById([storeO], [order({ id: "x", status: "filled", updatedTs: 9 }), order({ id: "y" })]);
  assert.equal(merged.length, 2);
  assert.equal(merged.find((o) => o.id === "x")?.status, "filled");
  assert.equal(mergeById([order({ id: "x", status: "open", updatedTs: 9 })], [order({ id: "x", status: "filled", updatedTs: 5 })])[0].status, "open");
  // fills have no updatedTs: the store copy wins
  assert.equal(mergeById([fill({ id: "f", qty: 1 })], [fill({ id: "f", qty: 2 })])[0].qty, 1);
  // a page that is not full holds everything
  assert.equal(pageCovers([30, 20], 3, null), true);
  // a full page holds the sample only when it reaches past the sample's start
  assert.equal(pageCovers([30, 20, 10], 3, null), false);
  assert.equal(pageCovers([30, 20, 10], 3, 15), true);
  assert.equal(pageCovers([30, 20, 10], 3, 10), false);
});

test("fills group by order oldest first; summary totals", () => {
  const fs = [fill({ id: "a", ts: 30 }), fill({ id: "b", ts: 10 }), fill({ id: "c", orderId: "o2", ts: 5, slippageCents: 2 })];
  const m = fillsByOrder(fs);
  assert.deepEqual(m.get("o1")?.map((f) => f.id), ["b", "a"]);
  const s = summarizeOrders([order({}), order({ id: "o2", feesUsd: 0.25 })], m);
  assert.equal(s.orders, 2);
  assert.equal(s.fills, 3);
  assert.equal(s.feesUsd, 0.75);
  assert.equal(s.feesUnknown, false);
  assert.equal(s.fillsMissing, 1); // o2 filled 100, one 50-contract fill loaded
  assert.equal(s.avgSlippage, Math.round((2 * 50 / 150) * 100) / 100);
  assert.equal(qtyText(96.24), "96.2");
  assert.equal(qtyText(100), "100");
});

// ---- windows ----

const win = (id: string, startTs: number, extra: Partial<MarketWindow> = {}): MarketWindow => ({
  id,
  title: id,
  startTs,
  endTs: startTs + 900_000,
  priceToBeat: 67000,
  upInstrument: `${id}-up`,
  downInstrument: `${id}-down`,
  status: "settled",
  result: "up",
  settlePrice: 67010,
  ...extra,
});

test("windowsInScope: this window / last 20 (+ current) / all, newest first, no future windows", () => {
  const base = 1_000_000_000_000;
  const ws = Array.from({ length: 30 }, (_, i) => win(`w${i}`, base + i * 900_000));
  const current = { ...ws[29], status: "live" as const };
  const next = win("next", base + 30 * 900_000, { status: "upcoming" });
  const now = current.startTs + 1000;
  assert.deepEqual(windowsInScope("window", current, [...ws, next], now).map((w) => w.id), ["w29"]);
  const last = windowsInScope("last20", current, [...ws, next], now);
  assert.equal(last.length, 21);
  assert.equal(last[0].id, "w29");
  assert.equal(last[20].id, "w9");
  const all = windowsInScope("all", current, [...ws, next], now);
  assert.equal(all.length, 30);
  assert.ok(!all.some((w) => w.id === "next"));
  assert.deepEqual(windowsInScope("window", null, ws, now).map((w) => w.id), ["w29"]);
});

test("mergeWindows prefers the fresher list", () => {
  const a = win("x", 1, { status: "settled", result: "down" });
  const b = win("x", 1, { status: "closed", result: null });
  const merged = mergeWindows([a], [b, win("y", 2)]);
  assert.equal(merged.length, 2);
  assert.equal(merged.find((w) => w.id === "x")?.result, "down");
});

test("per-window P&L = realized + unrealized of open positions; max |edge| keeps its sign", () => {
  const pos = (p: Partial<Position>): Position => ({
    id: "p",
    mode: "paper",
    windowId: "w1",
    instrument: "up",
    side: "up",
    qty: 0,
    avgCents: 30,
    costUsd: 0,
    realizedPnlUsd: 0,
    feesUsd: 0,
    status: "closed",
    markCents: null,
    unrealizedPnlUsd: null,
    bracketId: null,
    openedTs: 0,
    updatedTs: 0,
    closedTs: null,
    ...p,
  });
  const m = pnlByWindow(
    [
      pos({ id: "a", realizedPnlUsd: 5, feesUsd: 0.1 }),
      pos({ id: "b", status: "open", realizedPnlUsd: 1, unrealizedPnlUsd: 2.5, feesUsd: 0.1 }),
      pos({ id: "c", windowId: "w2", status: "settled", realizedPnlUsd: -3, unrealizedPnlUsd: 99, feesUsd: 0.1 }),
    ],
    "paper",
  );
  assert.deepEqual(m.get("w1"), { usd: 8.5, feeUnknown: false });
  assert.deepEqual(m.get("w2"), { usd: -3, feeUnknown: false });
  const e = maxEdgeByWindow([
    { windowId: "w1", edge: 2 },
    { windowId: "w1", edge: -4.5 },
    { windowId: "w1", edge: null },
    { windowId: "w2", edge: 1 },
  ]);
  assert.equal(e.get("w1"), -4.5);
  assert.equal(e.get("w2"), 1);
  // accumulates into an existing map (kept across a replay)
  const e2 = maxEdgeByWindow([{ windowId: "w2", edge: 3 }], new Map(e));
  assert.equal(e2.get("w2"), 3);
  assert.equal(e2.get("w1"), -4.5);
});

test("per-window P&L: one mode only, [FEE] when a fee may be unknown", () => {
  const pos = (p: Partial<Position>): Position => ({
    id: "p",
    mode: "paper",
    windowId: "w1",
    instrument: "up",
    side: "up",
    qty: 0,
    avgCents: 30,
    costUsd: 0,
    realizedPnlUsd: 0,
    feesUsd: 0.1,
    status: "closed",
    markCents: null,
    unrealizedPnlUsd: null,
    bracketId: null,
    openedTs: 0,
    updatedTs: 0,
    closedTs: null,
    ...p,
  });
  const ps = [pos({ id: "a", realizedPnlUsd: 2 }), pos({ id: "b", mode: "live", realizedPnlUsd: -50 }), pos({ id: "c", windowId: "w2", realizedPnlUsd: 1, feesUsd: 0 })];
  assert.deepEqual(pnlByWindow(ps, "paper").get("w1"), { usd: 2, feeUnknown: false });
  assert.deepEqual(pnlByWindow(ps, "live").get("w1"), { usd: -50, feeUnknown: false });
  assert.equal(pnlByWindow(ps, "live").get("w2"), undefined);
  // a loaded fill of the window / mode with an unknown fee flags it (a live fill does not flag paper)
  const fs = [fill({ windowId: "w1", mode: "live", feeUsd: null })];
  assert.equal(pnlByWindow(ps, "paper", fs).get("w1")?.feeUnknown, false);
  assert.equal(pnlByWindow(ps, "live", fs).get("w1")?.feeUnknown, true);
  // unconfirmed schedule: a position without any recorded fee may hide one
  assert.equal(pnlByWindow(ps, "paper", [], false).get("w2")?.feeUnknown, true);
  assert.equal(pnlByWindow(ps, "paper", [], false).get("w1")?.feeUnknown, false);
});

test("positionsCoverFrom: full pages and the snapshot's span limit the windows with a known P&L", () => {
  const now = 100 * 3600_000;
  const at = (openedTs: number) => ({ openedTs });
  assert.equal(positionsCoverFrom({ rows: [at(5), at(3)], limit: 1000 }, [], now), null);
  assert.equal(positionsCoverFrom({ rows: [at(5), at(3)], limit: 2 }, [], now), 3);
  // not fetched: the snapshot's 24h span, or its oldest row when it hit the 200 limit
  assert.equal(positionsCoverFrom(null, [at(now - 1000)], now), now - SNAPSHOT_POSITIONS_MS);
  const full = Array.from({ length: SNAPSHOT_POSITIONS_LIMIT }, (_, i) => at(now - 1000 - i));
  assert.equal(positionsCoverFrom(null, full, now), now - 1000 - (SNAPSHOT_POSITIONS_LIMIT - 1));
  assert.equal(earliestTs([{ ts: 9 }, { ts: 4 }, { ts: 7 }]), 4);
  assert.equal(earliestTs([]), null);
});

test("alertEditPatch sends only changed fields (a fired once-alert stays disabled)", () => {
  const init = { kind: "btc_above" as const, threshold: 67250, enabled: true, once: true, note: null };
  // only the note changed: enabled is NOT sent, so the backend's auto-disable survives
  assert.deepEqual(alertEditPatch(init, { ...init, note: "tp zone" }), { note: "tp zone" });
  assert.deepEqual(alertEditPatch(init, { ...init }), {});
  // kind and threshold travel together
  assert.deepEqual(alertEditPatch(init, { ...init, threshold: 68000 }), { kind: "btc_above", threshold: 68000 });
  assert.deepEqual(alertEditPatch(init, { ...init, kind: "btc_below" }), { kind: "btc_below", threshold: 67250 });
  assert.deepEqual(alertEditPatch(init, { ...init, enabled: false, once: false }), { enabled: false, once: false });
  // the dialog's own round trip of an unchanged alert is a no-op
  const f = formFromInput(init);
  const r = validateAlertForm(f);
  assert.ok(r.ok);
  if (r.ok) assert.deepEqual(alertEditPatch(r.input, r.input), {});
});

test("alert sound: blocked beeps are reported in the toast; the gesture unlock installs once", () => {
  assert.equal(alertToastMessage("BTC above $67,250.00", "blocked"), `BTC above $67,250.00 · ${SOUND_BLOCKED_HINT}`);
  assert.equal(alertToastMessage("BTC above $67,250.00", "played"), "BTC above $67,250.00");
  assert.equal(alertToastMessage("BTC above $67,250.00", null), "BTC above $67,250.00");
  assert.equal(playAlertSound(), "skipped"); // no WebAudio here
  const added: string[] = [];
  const removed: string[] = [];
  const handlers: ((e?: unknown) => void)[] = [];
  const target = {
    addEventListener: (t: string, h: (e?: unknown) => void, capture?: unknown) => {
      assert.equal(capture, true);
      added.push(t);
      handlers.push(h);
    },
    removeEventListener: (t: string) => void removed.push(t),
  } as unknown as Window;
  installAudioUnlock(target);
  installAudioUnlock(target); // idempotent
  assert.deepEqual(added.sort(), ["keydown", "pointerdown", "touchend"]);
  handlers[0](); // first gesture; no WebAudio -> nothing to wait for, listeners removed
  assert.deepEqual(removed.sort(), ["keydown", "pointerdown", "touchend"]);
});

test("ET day prefix only for other days", () => {
  const now = Date.UTC(2026, 9, 20, 16, 0); // 12:00 ET
  assert.equal(etDayPrefix(now - 3600_000, now), "");
  assert.equal(etDayPrefix(Date.UTC(2026, 9, 20, 2, 0), now), "Oct 19 · "); // 22:00 ET the day before
});

// ---- notifications ----

test("delivery plan: sound per pref, desktop only when unfocused + granted, toast otherwise", () => {
  const on = { desktop: true, sound: true };
  assert.deepEqual(deliveryPlan(on, "granted", false), { sound: true, desktop: true, toast: false });
  assert.deepEqual(deliveryPlan(on, "granted", true), { sound: true, desktop: false, toast: true });
  assert.deepEqual(deliveryPlan(on, "default", false), { sound: true, desktop: false, toast: true });
  assert.deepEqual(deliveryPlan(on, "denied", false), { sound: true, desktop: false, toast: true });
  assert.deepEqual(deliveryPlan({ desktop: false, sound: false }, "granted", false), { sound: false, desktop: false, toast: true });
  assert.deepEqual(deliveryPlan(on, "unsupported", false), { sound: true, desktop: false, toast: true });
});

test("scopeAnchor follows the live window during a replay", () => {
  const live = win("live", 2_000_000, { status: "live" });
  const old = win("old", 1_000_000);
  const windows = { live, old };
  assert.equal(scopeAnchor(old, windows, false)?.id, "old");
  assert.equal(scopeAnchor(old, windows, true)?.id, "live");
  assert.equal(scopeAnchor(old, { old }, true)?.id, "old");
});
