// Fixer tests: idempotent order retries (OrderKey / isAmbiguousFailure), REST-vs-WS ordering (newerOnly), the
// kill-switch "still open" warning, bracket re-suggestion on window / side change, and the settings save
// (diff-only patches, safe call order, partial-failure text).
// Run: tsx --test src/components/panels/__tests__/*.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import type { RiskPatch, RiskState, Settings, SettingsPatch } from "@shared/protocol";
import { DEFAULT_RISK_LIMITS } from "@shared/protocol";
import { ApiError } from "../../../lib/api-error";
import { OrderKey, isAmbiguousFailure, newerOnly, stillOpenText } from "../order-safety";
import { resuggestBracket } from "../ticket-math";
import { riskPatchFromForm, saveFailureText, saveSettingsForm, settingsPatchFromForm, toForm } from "../settings-form";

test("OrderKey reuses the key for the same draft and rotates on a new draft or after rotate()", () => {
  let n = 0;
  const k = new OrderKey(() => `k${++n}`);
  assert.equal(k.for("draft-a"), "k1");
  assert.equal(k.for("draft-a"), "k1"); // retry after a timeout: same key
  assert.equal(k.for("draft-b"), "k2"); // the user changed the draft
  k.rotate(); // definitive answer
  assert.equal(k.pending, null);
  assert.equal(k.for("draft-b"), "k3");
});

test("ambiguous failures: timeout, network, 5xx; definitive: 4xx and a declined live confirm", () => {
  const abort = new Error("aborted");
  abort.name = "AbortError";
  assert.equal(isAmbiguousFailure(abort), true);
  assert.equal(isAmbiguousFailure(new TypeError("Failed to fetch")), true);
  assert.equal(isAmbiguousFailure(new ApiError(502, "exchange_error", "bad gateway")), true);
  assert.equal(isAmbiguousFailure(new ApiError(500, null, "boom")), true);
  assert.equal(isAmbiguousFailure(new ApiError(403, "risk_rejected", "no")), false);
  assert.equal(isAmbiguousFailure(new ApiError(409, "no_market", "closed")), false);
  const cancelled = new Error("Live order cancelled");
  cancelled.name = "CancelledError";
  assert.equal(isAmbiguousFailure(cancelled), false);
});

test("newerOnly keeps WS records that are newer than a late REST response", () => {
  const cur = { p1: { id: "p1", updatedTs: 200, status: "closed" }, p2: { id: "p2", updatedTs: 100, status: "open" } };
  const incoming = [
    { id: "p1", updatedTs: 150, status: "open" }, // stale REST copy: dropped
    { id: "p2", updatedTs: 100, status: "open" }, // same version: applied
    { id: "p3", updatedTs: 50, status: "open" }, // unknown: applied
  ];
  assert.deepEqual(
    newerOnly(cur, incoming).map((x) => x.id),
    ["p2", "p3"],
  );
});

test("kill switch warning only when positions remain", () => {
  assert.equal(stillOpenText(0), null);
  assert.match(stillOpenText(1)!, /^1 position is still open/);
  assert.match(stillOpenText(2)!, /^2 positions are still open/);
});

test("bracket values are re-suggested for a new window or side, kept otherwise", () => {
  const basis = { windowId: "w1", side: "up" as const };
  assert.equal(resuggestBracket(basis, { windowId: "w1", side: "up", entryCents: 52, ptb: 67000 }), null);
  // next window: the BTC stop follows the new price to beat
  const nw = resuggestBracket(basis, { windowId: "w2", side: "up", entryCents: 50, ptb: 68123.4 })!;
  assert.equal(nw.patch.btcStop, "68123");
  assert.deepEqual(nw.basis, { windowId: "w2", side: "up" });
  // side flip: TP / SL from the Down entry
  const dn = resuggestBracket(basis, { windowId: "w1", side: "down", entryCents: 30, ptb: 67000 })!;
  assert.ok(Number(dn.patch.tpCents) > 30 && Number(dn.patch.slCents) < 30);
  // no quote yet: stale TP / SL are cleared and the basis stays open so they are suggested once a quote arrives
  const nq = resuggestBracket(basis, { windowId: "w2", side: "up", entryCents: null, ptb: 68000 })!;
  assert.equal(nq.patch.tpCents, "");
  assert.equal(nq.patch.slCents, "");
  assert.equal(nq.patch.btcStop, "68000");
  assert.equal(nq.basis, null);
  // after a submit (basis null) the next order gets fresh values
  assert.ok(resuggestBracket(null, { windowId: "w1", side: "up", entryCents: 60, ptb: 67000 }));
});

const settings = (over: Partial<Settings> = {}): Settings => ({
  mode: "paper",
  volLookbackMin: 30,
  feeCentsPerContract: 1.5,
  feeNote: "",
  risk: { ...DEFAULT_RISK_LIMITS },
  liveAvailable: true,
  ...over,
});

test("settings patch carries only changed keys", () => {
  const s = settings();
  const f = toForm(s);
  assert.deepEqual(settingsPatchFromForm(f, s), {});
  assert.deepEqual(riskPatchFromForm(f, s), {});
  assert.deepEqual(settingsPatchFromForm({ ...f, fee: "" }, s), { feeCentsPerContract: null });
  assert.deepEqual(settingsPatchFromForm({ ...f, mode: "live", vol: "45" }, s), { mode: "live", volLookbackMin: 45 });
  assert.deepEqual(riskPatchFromForm({ ...f, maxPositionUsd: "75" }, s), { maxPositionUsd: 75 });
  // env-default fee (null) with a blank field: nothing to send
  const s2 = settings({ feeCentsPerContract: null });
  assert.deepEqual(settingsPatchFromForm(toForm(s2), s2), {});
});

function io(fail: Partial<Record<"settings" | "risk", boolean>> = {}) {
  const calls: string[] = [];
  return {
    calls,
    io: {
      putSettings: async (p: SettingsPatch) => {
        calls.push(`settings:${JSON.stringify(p)}`);
        if (fail.settings) throw new ApiError(400, "validation", "bad settings");
        return settings(p as Partial<Settings>);
      },
      putRisk: async (p: RiskPatch) => {
        calls.push(`risk:${JSON.stringify(p)}`);
        if (fail.risk) throw new ApiError(503, null, "backend restarting");
        return { limits: { ...DEFAULT_RISK_LIMITS, ...p } } as unknown as RiskState;
      },
      onSettings: () => calls.push("onSettings"),
      onRisk: () => calls.push("onRisk"),
    },
  };
}

test("settings save: nothing changed = no call; risk-only = no PUT /api/settings", async () => {
  const s = settings();
  const a = io();
  assert.deepEqual(await saveSettingsForm(toForm(s), s, a.io), { applied: [], failed: null, error: null });
  assert.deepEqual(a.calls, []);
  const b = io();
  const out = await saveSettingsForm({ ...toForm(s), dailyLossLimitUsd: "40" }, s, b.io);
  assert.deepEqual(out.applied, ["risk"]);
  assert.deepEqual(b.calls, ['risk:{"dailyLossLimitUsd":40}', "onRisk"]);
});

test("settings save: switching to live applies risk first; a risk failure leaves the mode untouched", async () => {
  const s = settings();
  const f = { ...toForm(s), mode: "live" as const, maxExposureUsd: "100" };
  const ok = io();
  await saveSettingsForm(f, s, ok.io);
  assert.deepEqual(ok.calls, ['risk:{"maxExposureUsd":100}', "onRisk", 'settings:{"mode":"live"}', "onSettings"]);
  const bad = io({ risk: true });
  const out = await saveSettingsForm(f, s, bad.io);
  assert.equal(out.failed, "risk");
  assert.deepEqual(out.applied, []);
  assert.ok(!bad.calls.some((c) => c.startsWith("settings")), "mode must not switch to live");
  assert.equal(saveFailureText(out, "backend restarting"), "Risk limits NOT saved: backend restarting");
});

test("settings save: switching to paper applies the mode first and names a partial failure", async () => {
  const s = settings({ mode: "live" });
  const f = { ...toForm(s), mode: "paper" as const, maxPositionUsd: "20" };
  const r = io({ risk: true });
  const out = await saveSettingsForm(f, s, r.io);
  assert.deepEqual(r.calls.slice(0, 2), ['settings:{"mode":"paper"}', "onSettings"]);
  assert.deepEqual(out.applied, ["settings"]);
  assert.equal(out.failed, "risk");
  assert.equal(saveFailureText(out, "x"), "Mode, vol and fee saved. Risk limits NOT saved: x");
});
