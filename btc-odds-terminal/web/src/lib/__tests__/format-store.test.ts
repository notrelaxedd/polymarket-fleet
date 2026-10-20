import { test } from "node:test";
import assert from "node:assert/strict";
import { quote, quoteCents, spreadCents } from "../format";
import { selectFair, selectOdds } from "../store";
import { fair, odds, win } from "./helpers";

test("quote keeps the backend's 0.1¢ resolution and drops .0", () => {
  assert.equal(quote(56), "56");
  assert.equal(quote(55.5), "55.5");
  assert.equal(quote(55.300000000000004), "55.3");
  assert.equal(quote(55.99999999), "56");
  assert.equal(quote(null), "—");
  assert.equal(quoteCents(55.5), "55.5¢");
  assert.equal(quoteCents(undefined), "—");
});

test("spreadCents is rounded to 1 decimal (no float noise)", () => {
  assert.equal(spreadCents(55.3, 55.6), "0.3¢");
  assert.equal(spreadCents(51, 53), "2.0¢");
  assert.equal(spreadCents(null, 53), null);
});

test("selectOdds / selectFair ignore values from another window", () => {
  const cur = win(15 * 60_000, "live");
  const old = win(0, "closed");
  assert.equal(selectOdds({ current: cur, lastOdds: odds(old.id) }), null);
  assert.equal(selectFair({ current: cur, lastFair: fair(old.id) }), null);
  assert.equal(selectOdds({ current: null, lastOdds: odds(old.id) }), null);
  const o = odds(cur.id);
  const f = fair(cur.id);
  assert.equal(selectOdds({ current: cur, lastOdds: o }), o);
  assert.equal(selectFair({ current: cur, lastFair: f }), f);
});
