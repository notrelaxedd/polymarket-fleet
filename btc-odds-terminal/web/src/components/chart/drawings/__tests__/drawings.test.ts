// Unit tests for the drawing tools' pure logic: hit testing, drag math, fib / measure, text wrap,
// tool parsing, sticker SVG serialization and the debounced save queue. Run from web/:
//   NODE_OPTIONS=--conditions=import ../backend/node_modules/.bin/tsx --test src/components/chart/drawings/__tests__/*.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";
import type { Drawing, DrawingPoint } from "@shared/protocol";
import {
  distToSegment,
  fibLevels,
  fmtDuration,
  hitShape,
  hitTestAll,
  measureStats,
  moveBody,
  moveHandle,
  rayEnd,
  shapeBox,
  wrapText,
  type Projected,
} from "../geometry";
import { inputOf, isValidDrawing, newDrawing, validDrawings, parseTool, pointCount, roundPrice, sameInput, withAlpha } from "../model";
import { stickerSvg, svgMarkup } from "../stickerArt";
import { DrawingSync, MAX_OPS, isRetryable, mergeLists, planBatch, reqHttp, sameList, type SyncApi, type SyncStatus } from "../sync";
import { applyStep, diffLists, DrawingHistory } from "../history";
import { alertForLine, alertRefs, duplicateAlert } from "../alerting";
import * as React from "react";

const P = (time: number, price: number, scale: DrawingPoint["scale"] = "btc"): DrawingPoint => ({ time, price, scale });
const D = (kind: Drawing["kind"], points: DrawingPoint[], extra: Partial<Drawing> = {}): Drawing => ({
  ...newDrawing(kind, points, { now: 1 }),
  ...extra,
});
const view = { width: 800, height: 400 };

// ---- geometry -------------------------------------------------------------------------------------

test("distToSegment: perpendicular, beyond ends, degenerate", () => {
  assert.equal(distToSegment({ x: 5, y: 3 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 3);
  assert.equal(distToSegment({ x: 13, y: 4 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 5);
  assert.equal(distToSegment({ x: 3, y: 4 }, { x: 0, y: 0 }, { x: 0, y: 0 }), 5);
});

test("rayEnd extends to the pane edge (plus margin) in the ray's direction", () => {
  const e = rayEnd({ x: 100, y: 200 }, { x: 200, y: 200 }, view, 0);
  assert.deepEqual(e, { x: 800, y: 200 });
  const up = rayEnd({ x: 100, y: 200 }, { x: 200, y: 100 }, view, 0);
  assert.equal(up.y, 0);
  assert.equal(up.x, 300);
  // coincident points: nothing to extend
  assert.deepEqual(rayEnd({ x: 1, y: 1 }, { x: 1, y: 1 }, view), { x: 1, y: 1 });
});

test("hitShape: trend line within tolerance, ray beyond its second point, hline anywhere across", () => {
  const t: Projected = { d: D("trend", [P(0, 1), P(1, 2)]), pts: [{ x: 100, y: 100 }, { x: 300, y: 100 }] };
  assert.equal(hitShape(t, { x: 200, y: 104 }, 6, view, false)?.part, "body");
  assert.equal(hitShape(t, { x: 200, y: 110 }, 6, view, false), null);
  assert.equal(hitShape(t, { x: 500, y: 100 }, 6, view, false), null);
  const r: Projected = { ...t, d: D("ray", [P(0, 1), P(1, 2)]) };
  assert.equal(hitShape(r, { x: 600, y: 101 }, 6, view, false)?.part, "body");
  const h: Projected = { d: D("hline", [P(0, 1)]), pts: [{ x: -5000, y: 50 }] };
  assert.equal(hitShape(h, { x: 790, y: 53 }, 6, view, false)?.part, "body");
});

test("hitShape: handles of the selected drawing win over its body", () => {
  const t: Projected = { d: D("trend", [P(0, 1), P(1, 2)]), pts: [{ x: 100, y: 100 }, { x: 300, y: 200 }] };
  assert.equal(hitShape(t, { x: 302, y: 201 }, 6, view, true)?.part, "p1");
  assert.equal(hitShape(t, { x: 99, y: 99 }, 6, view, true)?.part, "p0");
  // not selected: the same spot is just the body
  assert.equal(hitShape(t, { x: 302, y: 201 }, 6, view, false)?.part, "body");
  const rect: Projected = { d: D("rect", [P(0, 1), P(1, 2)]), pts: [{ x: 100, y: 100 }, { x: 200, y: 150 }] };
  assert.equal(hitShape(rect, { x: 200, y: 100 }, 6, view, true)?.part, "c1");
  assert.equal(hitShape(rect, { x: 100, y: 150 }, 6, view, true)?.part, "c3");
  assert.equal(hitShape(rect, { x: 150, y: 120 }, 6, view, true)?.part, "body");
});

test("hitTestAll: topmost drawing wins, the selected one first", () => {
  const a: Projected = { d: D("rect", [P(0, 1), P(1, 2)], { id: "a" }), pts: [{ x: 0, y: 0 }, { x: 200, y: 200 }] };
  const b: Projected = { d: D("rect", [P(0, 1), P(1, 2)], { id: "b" }), pts: [{ x: 50, y: 50 }, { x: 150, y: 150 }] };
  assert.equal(hitTestAll([a, b], { x: 100, y: 100 }, 6, view, null)?.id, "b");
  assert.equal(hitTestAll([a, b], { x: 100, y: 100 }, 6, view, "a")?.id, "a");
  assert.equal(hitTestAll([a, b], { x: 400, y: 300 }, 6, view, null), null);
  const s: Projected = { d: D("sticker", [P(0, 1)], { id: "s" }), pts: [{ x: 500, y: 100 }], box: { left: 486, top: 86, right: 514, bottom: 114 } };
  assert.equal(hitTestAll([a, s], { x: 510, y: 110 }, 6, view, null)?.id, "s");
});

test("shapeBox: clipped to the pane, null when off-screen", () => {
  const t: Projected = { d: D("trend", [P(0, 1), P(1, 2)]), pts: [{ x: -100, y: 50 }, { x: 300, y: 500 }] };
  assert.deepEqual(shapeBox(t, view), { left: 0, top: 50, right: 300, bottom: 400 });
  const off: Projected = { ...t, pts: [{ x: 900, y: 50 }, { x: 1000, y: 60 }] };
  assert.equal(shapeBox(off, view), null);
});

test("moveBody: translates in data space; hline moves price only, vline time only; odds clamp 0–100", () => {
  const t = moveBody(D("trend", [P(1000, 100), P(2000, 110)]), 500, 2.5);
  assert.deepEqual(t.points.map((p) => [p.time, p.price]), [[1500, 102.5], [2500, 112.5]]);
  const h = moveBody(D("hline", [P(1000, 100)]), 500, 3);
  assert.deepEqual([h.points[0].time, h.points[0].price], [1000, 103]);
  const v = moveBody(D("vline", [P(1000, 100)]), 500, 3);
  assert.deepEqual([v.points[0].time, v.points[0].price], [1500, 100]);
  const o = moveBody(D("hline", [P(1000, 95, "odds")]), 0, 20);
  assert.equal(o.points[0].price, 100);
});

test("moveHandle: endpoints, Shift keeps trend level, rect corners own two coordinates", () => {
  const t = D("trend", [P(1000, 100), P(2000, 110)]);
  const m = moveHandle(t, "p1", 3000, 120);
  assert.deepEqual(m.points[1], P(3000, 120));
  assert.deepEqual(m.points[0], t.points[0]);
  assert.equal(moveHandle(t, "p1", 3000, 120, { level: true }).points[1].price, 100);
  const r = D("rect", [P(1000, 100), P(2000, 110)]);
  const c1 = moveHandle(r, "c1", 2500, 95);
  assert.deepEqual(c1.points.map((p) => [p.time, p.price]), [[1000, 95], [2500, 110]]);
  const c3 = moveHandle(r, "c3", 500, 130);
  assert.deepEqual(c3.points.map((p) => [p.time, p.price]), [[500, 100], [2000, 130]]);
  // input not mutated
  assert.deepEqual(r.points, [P(1000, 100), P(2000, 110)]);
});

test("fibLevels: level 0 at the second point, 1 at the first", () => {
  const lv = fibLevels(100, 200);
  assert.equal(lv[0].level, 0);
  assert.equal(lv[0].price, 200);
  assert.equal(lv[lv.length - 1].price, 100);
  const half = lv.find((l) => l.level === 0.5);
  assert.equal(half?.price, 150);
  const g = lv.find((l) => l.level === 0.618);
  assert.ok(g && Math.abs(g.price - 138.2) < 1e-9);
});

test("measureStats: delta, percent (BTC only), bars at the timeframe, duration", () => {
  const s = measureStats(P(0, 60000), P(90_000, 60300), 15);
  assert.equal(s.delta, 300);
  assert.equal(s.pct, 0.5);
  assert.equal(s.bars, 6);
  assert.equal(s.up, true);
  const o = measureStats(P(0, 60, "odds"), P(30_000, 52, "odds"), 5);
  assert.equal(o.pct, null);
  assert.equal(o.up, false);
  assert.equal(fmtDuration(45_000), "45s");
  assert.equal(fmtDuration(195_000), "3m 15s");
  assert.equal(fmtDuration(3_720_000), "1h 2m");
  assert.equal(fmtDuration(600_000), "10m");
});

test("wrapText: greedy words, explicit breaks, long words split, line cap with ellipsis", () => {
  const m = (s: string) => s.length * 10; // 10px per char
  assert.deepEqual(wrapText("aa bb cc", 50, m), ["aa bb", "cc"]);
  assert.deepEqual(wrapText("one\n\ntwo", 100, m), ["one", "", "two"]);
  assert.deepEqual(wrapText("abcdefghij", 40, m), ["abcd", "efgh", "ij"]);
  const capped = wrapText("a b c d e", 10, m, 2);
  assert.equal(capped.length, 2);
  assert.ok(capped[1].endsWith("…"));
});

// ---- model ----------------------------------------------------------------------------------------

test("parseTool / pointCount", () => {
  assert.deepEqual(parseTool("trend"), { kind: "trend", sticker: null });
  assert.deepEqual(parseTool("sticker:rocket"), { kind: "sticker", sticker: "rocket" });
  assert.equal(parseTool("sticker:nope"), null);
  assert.equal(parseTool("cursor"), null);
  assert.equal(pointCount("fib"), 2);
  assert.equal(pointCount("text"), 1);
});

test("newDrawing defaults, inputOf / sameInput ignore ids and timestamps, validation", () => {
  const a = newDrawing("sticker", [P(1, 2)], { sticker: "bull", now: 5 });
  assert.equal(a.sticker, "bull");
  assert.equal(a.color, "#E6E9EF");
  assert.equal(a.createdTs, 5);
  const b = { ...a, id: "other", updatedTs: 99 };
  assert.ok(sameInput(a, b));
  assert.ok(!sameInput(a, { ...a, color: "#34D399" }));
  assert.ok(!sameInput(a, { ...a, points: [P(1, 2.01)] }));
  assert.equal("id" in inputOf(a), false);
  assert.ok(isValidDrawing(a));
  assert.ok(!isValidDrawing({ ...a, kind: "trend" }));
  assert.equal(roundPrice(67012.4567, "btc"), 67012.46);
  assert.equal(roundPrice(-3, "odds"), 0);
  assert.equal(withAlpha("#5B9CFF", 0.5), "rgba(91, 156, 255, 0.5)");
});

test("sticker artwork serializes to standalone SVG", () => {
  const svg = stickerSvg("check", 32);
  assert.ok(svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24">'));
  assert.ok(svg.includes('fill="#22A06B"'));
  assert.ok(svg.includes("stroke-width="));
  assert.ok(!svg.includes("strokeWidth"));
  const frag = svgMarkup(
    React.createElement(React.Fragment, null, React.createElement("path", { d: "M0 0", strokeLinecap: "round" }), [
      React.createElement("circle", { key: "c", cx: 1, cy: 2, r: 3 }),
    ]),
  );
  assert.equal(frag, '<path d="M0 0" stroke-linecap="round"></path><circle cx="1" cy="2" r="3"></circle>');
});

// ---- hit priority (strokes before fills) ------------------------------------------------------------

test("hitShape: a rectangle's border is a stroke hit, its interior only a fill hit", () => {
  const rect: Projected = { d: D("rect", [P(0, 1), P(1, 2)], { id: "r" }), pts: [{ x: 100, y: 100 }, { x: 300, y: 300 }] };
  assert.equal(hitShape(rect, { x: 102, y: 200 }, 6, view, false, "stroke")?.fill, undefined);
  assert.equal(hitShape(rect, { x: 200, y: 200 }, 6, view, false, "stroke"), null);
  assert.equal(hitShape(rect, { x: 200, y: 200 }, 6, view, false, "fill")?.fill, true);
  assert.equal(hitShape(rect, { x: 200, y: 200 }, 6, view, false)?.fill, true);
});

test("hitTestAll: a trend line inside a later rectangle stays grabbable; fills come last", () => {
  const line: Projected = { d: D("trend", [P(0, 1), P(1, 2)], { id: "line" }), pts: [{ x: 120, y: 200 }, { x: 280, y: 200 }] };
  const rect: Projected = { d: D("rect", [P(0, 1), P(1, 2)], { id: "rect" }), pts: [{ x: 100, y: 100 }, { x: 300, y: 300 }] };
  const h = hitTestAll([line, rect], { x: 200, y: 202 }, 6, view, null);
  assert.equal(h?.id, "line");
  assert.ok(!h?.fill);
  // elsewhere inside the box: the rectangle's fill
  const f = hitTestAll([line, rect], { x: 200, y: 150 }, 6, view, null);
  assert.equal(f?.id, "rect");
  assert.equal(f?.fill, true);
});

test("hitShape: fib hits its level lines, not the space between them", () => {
  const fib: Projected = { d: D("fib", [P(0, 1), P(1, 2)], { id: "f" }), pts: [{ x: 100, y: 100 }, { x: 300, y: 300 }] };
  // level 0.5 at y = 200
  assert.equal(hitShape(fib, { x: 280, y: 201 }, 6, view, false, "stroke")?.part, "body");
  // between 0.236 (y≈252.8) and 0.382 (y≈223.6), away from the dashed anchor line
  assert.equal(hitShape(fib, { x: 280, y: 238 }, 6, view, false, "stroke"), null);
  assert.equal(hitShape(fib, { x: 280, y: 238 }, 6, view, false)?.fill, true);
  // painted level ys (log scale) override the linear ones
  assert.equal(hitShape({ ...fib, levelYs: [150] }, { x: 280, y: 151 }, 6, view, false, "stroke")?.part, "body");
});

// ---- drag range ---------------------------------------------------------------------------------------

test("moveBody refuses moves that leave a point out of range", () => {
  const s = D("sticker", [P(1_700_000_000_000, 100)]);
  assert.equal(moveBody(s, 0, -150), s); // BTC below zero
  assert.equal(moveBody(s, Number.NaN, 0), s);
  assert.equal(moveBody(s, -2_000_000_000_000, 0), s); // before the epoch
  assert.equal(moveBody(s, 1000, 5).points[0].price, 105);
  const o = D("trend", [P(1_700_000_000_000, 40, "odds"), P(1_700_000_060_000, 60, "odds")]);
  assert.deepEqual(moveBody(o, 0, 80).points.map((p) => p.price), [100, 100]); // odds clamp, still valid
});

// ---- validation ---------------------------------------------------------------------------------------

test("isValidDrawing: point count per kind, one scale, known kind / sticker", () => {
  assert.ok(!isValidDrawing(D("measure", [P(0, 1)])));
  assert.ok(!isValidDrawing(D("rect", [P(0, 1)])));
  assert.ok(!isValidDrawing(D("trend", [P(0, 1), P(1, 2, "odds")])));
  assert.ok(!isValidDrawing({ ...D("trend", [P(0, 1), P(1, 2)]), kind: "circle" as Drawing["kind"] }));
  assert.ok(!isValidDrawing({ ...D("sticker", [P(0, 1)]), sticker: "unicorn" as Drawing["sticker"] }));
  assert.ok(!isValidDrawing(null));
  assert.ok(!isValidDrawing({ ...D("hline", [P(0, 1)]), points: [{ time: 0, price: Number.NaN, scale: "btc" }] }));
  assert.ok(isValidDrawing(D("fib", [P(0, 1), P(1, 2)])));
  const good = [D("hline", [P(0, 1)])];
  assert.equal(validDrawings(good), good);
  assert.equal(validDrawings([...good, D("measure", [P(0, 1)])]).length, 1);
});

// ---- undo history (diffs) -------------------------------------------------------------------------------

const mk = (id: string, price = 100): Drawing => ({ ...D("hline", [P(0, price)]), id });

test("diffLists / applyStep: undo restores only what the step touched", () => {
  const before = [mk("a"), mk("b")];
  const after = [mk("a", 120), mk("c")];
  const step = diffLists(before, after);
  assert.deepEqual(step.map((c) => c.id).sort(), ["a", "b", "c"]);
  // meanwhile another tab added "z" and deleted nothing
  const now = [...after, mk("z")];
  const undone = applyStep(now, step, "undo");
  assert.deepEqual(undone.map((d) => d.id), ["a", "b", "z"]);
  assert.equal(undone[0].points[0].price, 100);
  assert.deepEqual(applyStep(undone, step, "redo").map((d) => d.id).sort(), ["a", "c", "z"]);
  // undo of an edit to a drawing deleted elsewhere does not resurrect it
  assert.deepEqual(applyStep([mk("c"), mk("z")], step, "undo").map((d) => d.id).sort(), ["b", "z"]);
});

test("DrawingHistory: record / undo / redo / remap", () => {
  const flags: string[] = [];
  const h = new DrawingHistory((u, r) => flags.push(`${u}${r}`));
  assert.equal(h.undo([]), null);
  h.record(diffLists([], [mk("local")]));
  h.remap("local", "srv");
  const afterUndo = h.undo([mk("srv"), mk("other")]);
  assert.deepEqual(afterUndo?.map((d) => d.id), ["other"]);
  assert.deepEqual(h.redo(afterUndo ?? [])?.map((d) => d.id).sort(), ["other", "srv"]);
  h.record([]);
  assert.ok(h.canUndo());
  assert.deepEqual(flags.slice(0, 3), ["truefalse", "falsetrue", "truefalse"]);
});

// ---- line alerts ----------------------------------------------------------------------------------------

test("alertForLine: direction from a fresh reference only; at the price it asks", () => {
  const line = { ...D("hline", [P(0, 70_000)]), label: " R1 " };
  assert.equal(alertForLine(line, 69_000, null)?.kind, "btc_above");
  assert.equal(alertForLine(line, 71_000, null)?.kind, "btc_below");
  assert.equal(alertForLine(line, 71_000, null)?.note, "R1");
  assert.equal(alertForLine(line, 70_000, null), null);
  assert.equal(alertForLine(line, null, 50), null);
  const odds = D("hline", [P(0, 60, "odds")]);
  assert.equal(alertForLine(odds, 70_000, 55)?.kind, "up_above");
  const now = 1_000_000;
  const tick = { ts: now - 1000, price: 69_000, sources: {} };
  const o = { ts: now - 2000, windowId: "w", upBid: 54, upAsk: 56, downBid: 44, downAsk: 46, upMid: 55 };
  assert.deepEqual(alertRefs({ connected: true, replaying: false, lastTick: tick, odds: o }, now), { btc: 69_000, upMid: 55 });
  // stale tick, disconnected feed, replay, no current-window quote
  assert.equal(alertRefs({ connected: true, replaying: false, lastTick: { ...tick, ts: now - 60_000 }, odds: o }, now).btc, null);
  assert.equal(alertRefs({ connected: false, replaying: false, lastTick: tick, odds: o }, now).btc, null);
  assert.equal(alertRefs({ connected: true, replaying: true, lastTick: tick, odds: o }, now).upMid, null);
  assert.equal(alertRefs({ connected: true, replaying: false, lastTick: tick, odds: null }, now).upMid, null);
});

test("duplicateAlert: same kind + threshold among enabled alerts", () => {
  const input = { kind: "btc_above" as const, threshold: 70_000, enabled: true, once: true, note: null };
  const a = { ...input, id: "x", lastFiredTs: null, createdTs: 0, updatedTs: 0 } as unknown as import("@shared/protocol").Alert;
  assert.ok(duplicateAlert([a], input));
  assert.equal(duplicateAlert([{ ...a, enabled: false }], input), null);
  assert.equal(duplicateAlert([{ ...a, threshold: 70_001 }], input), null);
  assert.equal(duplicateAlert([{ ...a, kind: "btc_below" }], input), null);
});

// ---- sync -----------------------------------------------------------------------------------------

test("planBatch: deletes, edits, creates; bulk restore replaces; deleting everything clears", () => {
  const a = mk("a");
  const b = mk("b");
  assert.deepEqual(planBatch([a, b], new Map([["b", { ...b, updatedTs: 77 }]])), []);
  const ops = planBatch([a, b], new Map<string, Drawing | null>([["b", null], ["a", { ...a, color: "#F87171" }], ["c", mk("c")]]));
  assert.deepEqual(ops.map((r) => `${r.kind}:${"id" in r ? r.id : ""}`), ["delete:b", "update:a", "create:c"]);
  // deleting an id the server never had needs no request
  assert.deepEqual(planBatch([a], new Map([["ghost", null]])), []);
  const many = Array.from({ length: MAX_OPS + 1 }, (_, i) => mk(`m${i}`));
  const restore = planBatch([a], new Map(many.map((d) => [d.id, d])));
  assert.equal(restore.length, 1);
  assert.equal(restore[0].kind, "replace");
  if (restore[0].kind === "replace") assert.deepEqual(restore[0].list.map((d) => d.id), ["a", ...many.map((d) => d.id)]);
  const clear = planBatch(many, new Map(many.map((d) => [d.id, null])));
  assert.deepEqual(clear.map((r) => r.kind), ["clear"]);
  // a few deletions stay individual (a drawing added elsewhere is never swept up)
  assert.deepEqual(planBatch([a, b], new Map([["a", null], ["b", null]])).map((r) => r.kind), ["delete", "delete"]);
});

test("mergeLists: server order, overlays by id, local additions appended", () => {
  const m = mergeLists([mk("a"), mk("b")], new Map([["b", null]]), new Map([["a", mk("a", 150)], ["x", mk("x")]]));
  assert.deepEqual(m.map((d) => `${d.id}:${d.points[0].price}`), ["a:150", "x:100"]);
});

test("isRetryable: network / timeout / 429 / 5xx retry; 4xx and 501 do not", () => {
  assert.ok(isRetryable(new Error("Failed to fetch")));
  assert.ok(isRetryable({ status: 503 }));
  assert.ok(isRetryable({ status: 429 }));
  assert.ok(isRetryable({ status: 408 }));
  assert.ok(!isRetryable({ status: 400 }));
  assert.ok(!isRetryable({ status: 404 }));
  assert.ok(!isRetryable({ status: 501 }));
});

test("reqHttp: keepalive payloads", () => {
  const d = mk("a b");
  assert.deepEqual(reqHttp({ kind: "delete", id: "a b" }), { method: "DELETE", path: "/drawings/a%20b" });
  const c = reqHttp({ kind: "create", id: "x", d });
  assert.equal(c.method, "POST");
  assert.equal("id" in (c.body as object), false);
  assert.equal(reqHttp({ kind: "replace", list: [d], ids: ["a b"] }).path, "/drawings");
});

interface Harness {
  sync: DrawingSync;
  calls: string[];
  statuses: SyncStatus[];
  remaps: string[];
  list: () => Drawing[];
  setLocal: (l: Drawing[]) => void;
  echo: (l: Drawing[]) => void;
  tick: () => Promise<void>;
  timers: () => number;
  fail: (n: number, status?: number | null) => void;
  server: () => Drawing[];
  setServer: (l: Drawing[]) => void;
}

function harness(initial: Drawing[] = [], opts: { serverList?: Drawing[]; hydrated?: boolean; listFails?: number | null } = {}): Harness {
  let list = initial;
  let server: Drawing[] = opts.serverList ?? initial;
  const subs = new Set<(l: Drawing[]) => void>();
  const setList = (l: Drawing[]) => {
    list = l;
    for (const f of subs) f(l);
  };
  const timers: { fn: () => void; ms: number }[] = [];
  const calls: string[] = [];
  const statuses: SyncStatus[] = [];
  const remaps: string[] = [];
  let failures = 0;
  let failStatus: number | null = 500;
  let seq = 0;
  const call = async (s: string) => {
    calls.push(s);
    if (failures > 0) {
      failures--;
      throw failStatus == null ? new Error("Failed to fetch") : Object.assign(new Error(`boom ${failStatus}`), { status: failStatus });
    }
  };
  const api: SyncApi = {
    list: async () => {
      calls.push("list");
      if (opts.listFails != null) throw Object.assign(new Error("nope"), { status: opts.listFails });
      return server;
    },
    createDrawing: async (input) => {
      await call(`create:${input.kind}`);
      const d = { ...input, id: `s${++seq}`, createdTs: 1, updatedTs: 1 } as Drawing;
      server = [...server, d];
      return d;
    },
    replaceDrawings: async (l) => {
      await call(`replace:${l.map((d) => d.id).join(",")}`);
      server = l;
    },
    updateDrawing: async (id, patch) => {
      await call(`update:${id}`);
      const cur = server.find((d) => d.id === id);
      if (!cur) throw Object.assign(new Error("gone"), { status: 404 });
      const d = { ...cur, ...patch, updatedTs: 10 + ++seq } as Drawing;
      server = server.map((x) => (x.id === id ? d : x));
      return d;
    },
    deleteDrawing: async (id) => {
      await call(`delete:${id}`);
      server = server.filter((d) => d.id !== id);
    },
    deleteAllDrawings: async () => {
      await call("clear");
      server = [];
    },
  };
  const sync = new DrawingSync(
    api,
    { get: () => list, set: setList, subscribe: (f) => (subs.add(f), () => subs.delete(f)) },
    {
      hydrated: opts.hydrated ?? true,
      maxAttempts: 3,
      onStatus: (s) => statuses.push(s),
      onRemap: (a, b) => remaps.push(`${a}->${b}`),
      setTimer: (fn, ms) => {
        const t = { fn, ms };
        timers.push(t);
        return t;
      },
      clearTimer: (h) => {
        const i = timers.indexOf(h as (typeof timers)[number]);
        if (i >= 0) timers.splice(i, 1);
      },
    },
  ).start();
  const flushMicro = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };
  return {
    sync,
    calls,
    statuses,
    remaps,
    list: () => list,
    setLocal: (l) => sync.local(() => setList(l)),
    echo: (l) => setList(l),
    tick: async () => {
      await flushMicro();
      const t = timers.shift();
      t?.fn();
      await flushMicro();
    },
    timers: () => timers.length,
    fail: (n, status = 500) => {
      failures = n;
      failStatus = status;
    },
    server: () => server,
    setServer: (l) => {
      server = l;
    },
  };
}

test("DrawingSync debounces a burst of local edits into one request", async () => {
  const h = harness([mk("a")]);
  h.setLocal([{ ...mk("a"), color: "#34D399" }]);
  h.setLocal([{ ...mk("a"), color: "#F87171" }]);
  assert.equal(h.calls.length, 0);
  await h.tick();
  assert.deepEqual(h.calls, ["update:a"]);
  assert.equal(h.sync.pending(), false);
  assert.equal(h.sync.status(), "saved");
});

test("DrawingSync: an addition is a POST; the server id replaces the local one", async () => {
  const h = harness([mk("a")]);
  const local = { ...mk("local"), color: "#34D399" };
  h.setLocal([mk("a"), local]);
  await h.tick();
  assert.deepEqual(h.calls, ["create:hline"]);
  assert.deepEqual(h.remaps, ["local->s1"]);
  assert.deepEqual(h.list().map((d) => d.id), ["a", "s1"]);
  assert.equal(h.list()[1].color, "#34D399");
  // an edit of it afterwards is a PUT of the server id (no second POST), even before the echo arrives
  h.setLocal([mk("a"), { ...h.list()[1], color: "#F87171" }]);
  await h.tick();
  assert.deepEqual(h.calls, ["create:hline", "update:s1"]);
});

test("DrawingSync: an edit made before the first server list never hides or deletes server drawings", async () => {
  // the store is empty at mount; the server already has a and b
  const h = harness([], { hydrated: false, serverList: [mk("a"), mk("b")] });
  // the GET is in flight; the user adds x meanwhile
  h.setLocal([mk("x")]);
  assert.equal(h.sync.status(), "loading");
  assert.deepEqual(h.calls, ["list"]); // nothing is saved before the server list is known
  await h.tick(); // GET resolves -> merged, then the held edit is sent as a POST
  assert.deepEqual(h.list().map((d) => d.id).sort(), ["a", "b", "s1"]);
  assert.deepEqual(h.calls, ["list", "create:hline"]);
  assert.deepEqual(h.server().map((d) => d.id).sort(), ["a", "b", "s1"]);
});

test("DrawingSync: a snapshot after a reconnect merges with pending local edits by id", async () => {
  const h = harness([mk("a")]);
  h.setLocal([mk("a", 150)]); // pending edit of a
  // reconnect snapshot: the server has a (old) plus z from another tab
  h.echo([mk("a", 100), mk("z")]);
  assert.deepEqual(h.list().map((d) => `${d.id}:${d.points[0].price}`), ["a:150", "z:100"]);
  await h.tick();
  assert.deepEqual(h.calls, ["update:a"]);
  // with nothing pending a newer server list (higher server updatedTs) is accepted as is
  const newer = [{ ...mk("a", 175), updatedTs: 999 }, mk("z")];
  h.echo(newer);
  assert.ok(sameList(h.list(), newer));
});

test("DrawingSync: a stale echo after a confirmed save does not flicker the edit back", async () => {
  const h = harness([mk("a", 100)]);
  h.setLocal([mk("a", 150)]);
  await h.tick();
  h.echo([mk("a", 100)]); // older echo arrives late
  assert.equal(h.list()[0].points[0].price, 150);
});

test("DrawingSync: an edit of a drawing deleted elsewhere re-creates it (404 -> POST)", async () => {
  const h = harness([mk("a")]);
  h.setServer([]);
  h.setLocal([mk("a", 130)]);
  await h.tick();
  assert.deepEqual(h.calls, ["update:a", "create:hline"]);
  assert.deepEqual(h.remaps, ["a->s1"]);
});

test("DrawingSync retries a transient failure with backoff and keeps the local list", async () => {
  const h = harness([mk("a")]);
  h.fail(1, null);
  h.setLocal([mk("a"), mk("b")]);
  await h.tick();
  assert.equal(h.calls.length, 1);
  assert.equal(h.sync.pending(), true);
  assert.equal(h.sync.status(), "retrying");
  assert.equal(h.list().length, 2);
  await h.tick(); // backoff timer
  assert.deepEqual(h.calls, ["create:hline", "create:hline"]);
  assert.equal(h.sync.pending(), false);
  assert.equal(h.sync.status(), "saved");
});

test("DrawingSync: retries are capped (offline), a new edit or server list tries again", async () => {
  const h = harness([mk("a")]);
  h.fail(99, 503);
  h.setLocal([mk("a", 120)]);
  await h.tick();
  await h.tick();
  await h.tick();
  assert.equal(h.calls.length, 3);
  assert.equal(h.sync.status(), "offline");
  assert.equal(h.timers(), 0);
  await h.sync.whenIdle(); // resolves: saving gave up
  assert.equal(h.list()[0].points[0].price, 120); // kept on the page
  h.fail(0);
  h.echo([mk("a")]); // the server answers again (reconnect snapshot)
  assert.equal(h.list()[0].points[0].price, 120);
  await h.tick();
  assert.deepEqual(h.calls.slice(-1), ["update:a"]);
  assert.equal(h.sync.status(), "saved");
});

test("DrawingSync: a rejected change (4xx / 501) is reverted, not retried, and later edits still save", async () => {
  const h = harness([mk("a")]);
  h.fail(1, 400);
  h.setLocal([mk("a"), mk("b")]);
  await h.tick();
  assert.deepEqual(h.calls, ["create:hline"]);
  assert.equal(h.timers(), 0);
  assert.equal(h.sync.pending(), false);
  assert.deepEqual(h.list().map((d) => d.id), ["a"]); // back to the server's list
  assert.equal(h.sync.status(), "rejected");
  // server updates are no longer pinned under a poisoned local list
  h.echo([mk("a", 101)]);
  assert.equal(h.list()[0].points[0].price, 101);
  h.setLocal([mk("a", 140)]);
  await h.tick();
  assert.deepEqual(h.calls, ["create:hline", "update:a"]);
  assert.equal(h.sync.status(), "saved");
});

test("DrawingSync: a 501 drawings API loads as the store list and rejects saves", async () => {
  const h = harness([], { hydrated: false, listFails: 501 });
  await h.tick();
  assert.equal(h.sync.ready(), true);
  assert.equal(h.sync.status(), "rejected");
  h.fail(1, 501);
  h.setLocal([mk("x")]);
  await h.tick();
  assert.deepEqual(h.list(), []);
  assert.equal(h.timers(), 0);
});

test("DrawingSync: no request when an edit is undone before the debounce fires", async () => {
  const base = [mk("a")];
  const h = harness(base);
  h.setLocal([mk("a", 120)]);
  h.setLocal(base);
  await h.tick();
  assert.deepEqual(h.calls, []);
});

test("DrawingSync: deleting everything clears; undoing it restores with one replace", async () => {
  const many = Array.from({ length: MAX_OPS + 1 }, (_, i) => mk(`m${i}`));
  const h = harness(many);
  h.setLocal([]);
  await h.tick();
  assert.deepEqual(h.calls, ["clear"]);
  h.setLocal(many);
  await h.tick();
  assert.deepEqual(h.calls, ["clear", `replace:${many.map((d) => d.id).join(",")}`]);
  assert.deepEqual(h.remaps, []);
});

test("DrawingSync.takeUnloadRequests: queued edits as requests, then nothing pending", () => {
  const h = harness([mk("a")]);
  h.setLocal([mk("a", 111), mk("n")]);
  const reqs = h.sync.takeUnloadRequests();
  assert.deepEqual(reqs.map((r) => r.kind), ["update", "create"]);
  assert.equal(h.sync.pending(), false);
  assert.equal(h.timers(), 0);
  assert.deepEqual(h.sync.takeUnloadRequests(), []);
});
