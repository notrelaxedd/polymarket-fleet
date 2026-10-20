// Order ticket math: book-walk estimate, validation, request building, bracket suggestions, risk bars,
// block reasons and the settings form. Run: tsx --test src/components/panels/__tests__/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import type { BookLevels } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { DEFAULT_TICKET, type TicketDraft } from "../../../lib/store";
import {
  blockReason,
  bracketFromDraft,
  buildOrderRequest,
  estimateTicket,
  fieldsFromBracket,
  parseNum,
  riskBar,
  sanitizeDecimal,
  suggestBracket,
  validateBracket,
  validateTicket,
  type EstimateInput,
} from "../ticket-math";
import { toForm, validateSettingsForm } from "../settings-form";

const book: Pick<BookLevels, "bids" | "asks"> = {
  bids: [
    { price: 24, size: 100 },
    { price: 23, size: 200 },
  ],
  asks: [
    { price: 26, size: 50 },
    { price: 27, size: 100 },
    { price: 28, size: 1000 },
  ],
};
const odds = { upBid: 24, upAsk: 26, downBid: 74, downAsk: 76 };
const base: EstimateInput = {
  side: "up",
  type: "marketable",
  usd: 25,
  limitCents: null,
  maxSlippageCents: 2,
  book,
  odds,
  fairUp: 30,
  feeCentsPerContract: null,
};
const draft = (p: Partial<TicketDraft> = {}): TicketDraft => ({ ...DEFAULT_TICKET, ...p });
const close = (a: number | null | undefined, b: number, eps = 1e-6) => assert.ok(a != null && Math.abs(a - b) < eps, `${a} != ${b}`);

test("parseNum / sanitizeDecimal", () => {
  assert.equal(parseNum(""), null);
  assert.equal(parseNum("."), null);
  assert.equal(parseNum(" 12.5 "), 12.5);
  assert.equal(parseNum("abc"), null);
  assert.equal(sanitizeDecimal("1a2.3.4$"), "12.34");
  assert.equal(sanitizeDecimal("..5"), ".5");
});

test("marketable buy walks the Up asks within ask + slippage", () => {
  const e = estimateTicket(base)!;
  // $25: 50 @ 26 = $13, then $12 / 0.27 = 44.4 @ 27 = $11.988
  assert.equal(e.ask, 26);
  assert.equal(e.bid, 24);
  assert.equal(e.limitCents, 28);
  close(e.fillQty, 94.4);
  assert.equal(e.restQty, 0);
  close(e.costUsd, 24.99);
  close(e.avgCents!, (24.99 * 100) / 94.4);
  assert.equal(e.feeUsd, null);
  close(e.maxLossUsd, 24.99);
  close(e.payoutUsd, 94.4);
  close(e.profitUsd, 94.4 - 24.99);
  close(e.edgeCents!, 30 - (24.99 * 100) / 94.4);
  assert.equal(e.partial, false);
  assert.equal(e.depth, true);
});

test("marketable buy beyond the cap is partial (IOC)", () => {
  const e = estimateTicket({ ...base, usd: 500, maxSlippageCents: 1 })!;
  close(e.fillQty, 150); // 50 @ 26 + 100 @ 27, 28 is beyond 26 + 1
  assert.equal(e.partial, true);
  assert.equal(e.restQty, 0);
});

test("Down side mirrors the Up book (Down asks = 100 - Up bids)", () => {
  const e = estimateTicket({ ...base, side: "down", usd: 7.6 })!;
  assert.equal(e.ask, 76);
  assert.equal(e.bid, 74);
  close(e.fillQty, 10);
  close(e.edgeCents!, 70 - 76);
});

test("limit below the ask rests; at/above the ask fills what crosses and rests the remainder", () => {
  const rest = estimateTicket({ ...base, type: "limit", limitCents: 20, usd: 10 })!;
  assert.equal(rest.fillQty, 0);
  close(rest.restQty, 50);
  close(rest.avgCents!, 20);
  const mixed = estimateTicket({ ...base, type: "limit", limitCents: 26, usd: 20 })!;
  close(mixed.fillQty, 50); // $13 at 26
  close(mixed.restQty, 26.9); // $7 / 0.26
  close(mixed.qty, 76.9);
  assert.equal(mixed.partial, false);
  // empty limit = join the ask
  assert.equal(estimateTicket({ ...base, type: "limit" })!.limitCents, 26);
});

test("fees use the override per contract; no book falls back to top of book", () => {
  const e = estimateTicket({ ...base, feeCentsPerContract: 1, usd: 13 })!;
  close(e.fillQty, 50);
  close(e.feeUsd!, 0.5);
  close(e.maxLossUsd, 13.5);
  close(e.profitUsd, 50 - 13.5);
  const tob = estimateTicket({ ...base, book: null, usd: 26 })!;
  assert.equal(tob.depth, false);
  close(tob.fillQty, 100);
  assert.equal(estimateTicket({ ...base, book: null, odds: null }), null);
});

test("validateTicket: amount, slippage and limit", () => {
  const est = estimateTicket(base);
  const ctx = { est, btc: 67000, secLeft: 400, limits: DEFAULT_RISK_LIMITS };
  assert.deepEqual(validateTicket(draft(), ctx), {});
  assert.ok(validateTicket(draft({ usd: "" }), ctx).usd);
  assert.ok(validateTicket(draft({ usd: "0.5" }), ctx).usd);
  assert.match(validateTicket(draft({ usd: "300" }), ctx).usd!, /max position \$250/);
  assert.ok(validateTicket(draft({ maxSlippageCents: "" }), ctx).maxSlippageCents);
  assert.ok(validateTicket(draft({ type: "limit", limitCents: "100" }), ctx).limitCents);
  assert.equal(validateTicket(draft({ type: "marketable", limitCents: "100" }), ctx).limitCents, undefined);
});

test("validateTicket: bracket legs relative to entry, bid, BTC and time left", () => {
  const est = estimateTicket(base); // avg ~26.5, bid 24
  const ctx = { est, btc: 67000, secLeft: 400, limits: null };
  const on = (p: Partial<TicketDraft>) => validateTicket(draft({ bracketOn: true, tpCents: "40", slCents: "15", ...p }), ctx);
  assert.deepEqual(on({}), {});
  assert.match(on({ tpCents: "26" }).tpCents!, /above the entry/);
  assert.match(on({ slCents: "24" }).slCents!, /would exit at once/);
  assert.match(on({ slCents: "30" }).slCents!, /below the entry/);
  assert.ok(on({ tpCents: "" }).tpCents);
  assert.equal(on({ tpOn: false, tpCents: "" }).tpCents, undefined);
  assert.equal(on({ btcStopOn: true, btcStop: "66900" }).btcStop, undefined);
  assert.match(on({ btcStopOn: true, btcStop: "67100" }).btcStop!, /already below/);
  assert.match(validateTicket(draft({ side: "down", bracketOn: true, tpOn: false, slOn: false, btcStopOn: true, btcStop: "66900" }), { ...ctx, est: estimateTicket({ ...base, side: "down" }) }).btcStop!, /already above/);
  assert.match(on({ timeExitOn: true, timeExitSec: "500" }).timeExitSec!, /Less time left/);
  assert.ok(on({ timeExitOn: true, timeExitSec: "2.5" }).timeExitSec);
  assert.equal(on({ timeExitOn: true, timeExitSec: "30" }).timeExitSec, undefined);
  // bracket off: legs are not checked
  assert.deepEqual(validateTicket(draft({ bracketOn: false, tpCents: "1" }), ctx), {});
});

test("validateBracket for an open position skips the entry check but not the bid check", () => {
  const f = { ...fieldsFromBracket({ tpCents: 20, slCents: 10, btcStop: null, timeExitSec: null }) };
  assert.deepEqual(validateBracket(f, "up", { entry: null, bid: 15, btc: null, secLeft: null }), {});
  assert.ok(validateBracket(f, "up", { entry: null, bid: 21, btc: null, secLeft: null }).tpCents);
  assert.match(validateBracket({ ...f, tpCents: "9" }, "up", { entry: null, bid: null, btc: null, secLeft: null }).tpCents!, /above the stop loss/);
});

test("buildOrderRequest / bracketFromDraft", () => {
  const r = buildOrderRequest(draft({ side: "down", type: "limit", usd: "50", limitCents: "", maxSlippageCents: "3" }), {
    clientOrderId: "c1",
    fallbackLimitCents: 41,
  });
  assert.deepEqual(r, { side: "down", type: "limit", usd: 50, limitCents: 41, maxSlippageCents: 3, bracket: null, liveSession: null, clientOrderId: "c1" });
  const m = buildOrderRequest(draft({ type: "marketable", limitCents: "33", bracketOn: true, tpCents: "60", slCents: "", slOn: false, timeExitOn: true, timeExitSec: "45" }));
  assert.equal(m.limitCents, null);
  assert.deepEqual(m.bracket, { tpCents: 60, slCents: null, btcStop: null, timeExitSec: 45, maxSlippageCents: 2 });
  assert.equal(bracketFromDraft(draft({ bracketOn: true, tpOn: false, slOn: false })), null);
});

test("suggestBracket keeps TP/SL inside 1-99 and at least 3c away", () => {
  assert.deepEqual(suggestBracket(26, 67012.4), { tpCents: "44", slCents: "20", btcStop: "67012", timeExitSec: "30" });
  assert.deepEqual(suggestBracket(97, null), { tpCents: "99", slCents: "73", btcStop: "", timeExitSec: "30" });
  assert.equal(suggestBracket(2, null).slCents, "1");
  // with the bid known the SL stays below it, or is left blank when there is no room
  assert.equal(suggestBracket(2, null, 1).slCents, "");
  assert.equal(suggestBracket(3, null, 2).slCents, "1");
  assert.equal(suggestBracket(98, null, 99).tpCents, "");
  assert.equal(suggestBracket(null, null).tpCents, "");
});

test("riskBar tones and clamping", () => {
  assert.deepEqual(riskBar(0, 200), { pct: 0, tone: "ok" });
  assert.deepEqual(riskBar(120, 200), { pct: 60, tone: "warn" });
  assert.deepEqual(riskBar(190, 200), { pct: 95, tone: "danger" });
  assert.deepEqual(riskBar(500, 200), { pct: 100, tone: "danger" });
  assert.deepEqual(riskBar(-5, 200), { pct: 0, tone: "ok" });
});

test("blockReason priority", () => {
  const ok = { replayActive: false, hasWindow: true, hasQuote: true, risk: null, now: 1000 };
  assert.equal(blockReason(ok), null);
  assert.match(blockReason({ ...ok, replayActive: true, hasQuote: false })!, /replay/);
  assert.match(blockReason({ ...ok, risk: { killSwitch: true, tradingDisabledUntil: null, disabledReason: null } })!, /Kill switch/);
  assert.equal(blockReason({ ...ok, risk: { killSwitch: false, tradingDisabledUntil: 2000, disabledReason: "Daily loss limit reached" } }), "Daily loss limit reached");
  assert.equal(blockReason({ ...ok, risk: { killSwitch: false, tradingDisabledUntil: 500, disabledReason: "old" } }), null);
  assert.match(blockReason({ ...ok, hasWindow: false })!, /window/);
  assert.match(blockReason({ ...ok, hasQuote: false })!, /quote/);
});

test("settings form validation", () => {
  const s = { mode: "paper" as const, volLookbackMin: 30, feeCentsPerContract: null, feeNote: "", liveAvailable: false };
  const f = toForm(s);
  assert.equal(f.fee, "");
  assert.equal(f.dailyLossLimitUsd, "200");
  assert.deepEqual(validateSettingsForm(f, false), {});
  assert.ok(validateSettingsForm({ ...f, mode: "live" }, false).mode);
  assert.deepEqual(validateSettingsForm({ ...f, mode: "live" }, true), {});
  assert.ok(validateSettingsForm({ ...f, vol: "2" }, false).vol);
  assert.ok(validateSettingsForm({ ...f, fee: "60" }, false).fee);
  assert.ok(validateSettingsForm({ ...f, maxPositionUsd: "0" }, false).maxPositionUsd);
  assert.equal(toForm({ ...s, feeCentsPerContract: 1.5 }).fee, "1.5");
});
