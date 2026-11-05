// Extra traffic and days for a panel admin account: one-tap amounts and a typed amount in range.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness } from "./helpers/rename-screen.mjs";

const h = screenHarness();
const extras = h.compile(new URL("../src/app/agency-extras.ts", import.meta.url), {});
const locale = (name) => JSON.parse(readFileSync(new URL(`../src/app/locales/${name}.json`, import.meta.url), "utf8"));

test("one-tap amounts stay inside the shop's range", () => {
  assert.deepEqual(extras.stepsWithin(extras.GB_STEPS, [1, 1000]), [10, 50, 100, 200, 500]);
  assert.deepEqual(extras.stepsWithin(extras.GB_STEPS, [20, 150]), [50, 100]);
  assert.deepEqual(extras.stepsWithin(extras.DAY_STEPS, [1, 5]), [1, 5], "no step fits: the ends");
  assert.deepEqual(extras.stepsWithin(extras.DAY_STEPS, [3, 3]), [3]);
});

test("a typed amount: any digits, inside the range only", () => {
  assert.equal(extras.parseAmount("25", [1, 30]), 25);
  assert.equal(extras.parseAmount("۲۵", [1, 30]), 25);
  assert.equal(extras.parseAmount(" 30 ", [1, 30]), 30);
  assert.equal(extras.parseAmount("31", [1, 30]), null);
  assert.equal(extras.parseAmount("0", [1, 30]), null);
  assert.equal(extras.parseAmount("2.5", [1, 30]), null);
  assert.equal(extras.parseAmount("", [1, 30]), null);
});

test("every new Mini App label exists in both languages", () => {
  const fa = locale("fa"), en = locale("en");
  for (const key of ["agency_add_days", "checkout_account_days", "agency_custom_amount", "agency_amount_range", "err_acc_amount_range", "n_days", "agency_renew_custom", "agency_renew_size", "err_renew_option_unavailable", "unlimited_data"]) {
    assert.ok(key in fa, `fa is missing ${key}`);
    assert.ok(key in en, `en is missing ${key}`);
  }
});
