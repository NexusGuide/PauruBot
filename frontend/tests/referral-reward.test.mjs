// Referral reward config: progress toward a free config, and taking it from the Mini App.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness, nodes } from "./helpers/rename-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const text = tree => nodes(tree, () => true).flatMap(n => [n.props.children].flat()).filter(v => typeof v === "string").join(" ");
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };

function setup(claim) {
  const h = screenHarness(), calls = { claims: 0, toasts: [], reloads: 0 };
  const { ReferralReward } = h.compile(url("screens/ReferralReward.tsx"), {
    "@shared/format": { money: String },
    "../api": { api: { claimReferral: async () => { calls.claims++; return claim(); } } },
    "../state": {
      useApp: () => ({ t: (k, v) => v ? `${k}:${JSON.stringify(v)}` : k, locale: "en",
        toast: (m, tone) => calls.toasts.push([m, tone]), reload: async () => { calls.reloads++; } }),
      errorText: (_t, err) => `err:${err.message}`,
    },
    "../telegram": { haptic: () => {} },
    "../ui": { Button: "Button", Panel: "Panel" },
  });
  return { h, ReferralReward, calls };
}
const config = (over = {}) => ({ every: 5, counted: 3, progress: 3, available: 0, counts: "purchase",
  reward: { plan_title: null, gb: 5, days: 30 }, ...over });

test("shows the rule, the size and the progress, with no button until a config is ready", () => {
  const { h, ReferralReward } = setup(() => assert.fail());
  const tree = h.render(ReferralReward, { config: config() });
  const shown = text(tree);
  assert.match(shown, /referral_config_rule/); assert.match(shown, /referral_config_size/);
  assert.match(shown, /referral_config_progress:\{"progress":"3","every":"5"\}/);
  assert.equal(nodes(tree, n => n.type === "Button").length, 0);
  assert.equal(nodes(tree, n => n.props?.role === "progressbar")[0].props["aria-valuenow"], 3);
});

test("a join-counted rule and a plan reward read as such", () => {
  const { h, ReferralReward } = setup(() => assert.fail());
  const shown = text(h.render(ReferralReward, { config: config({ counts: "join", reward: { plan_title: "Gold", gb: 50, days: 30 } }) }));
  assert.match(shown, /referral_config_rule_join/); assert.match(shown, /Gold/);
});

test("a ready config is taken once, then the app reloads", async () => {
  const { h, ReferralReward, calls } = setup(async () => ({ order_id: 1, cash: false, config: null }));
  const ready = config({ available: 1, progress: 0 });
  const [button] = nodes(h.render(ReferralReward, { config: ready }), n => n.type === "Button");
  button.props.onClick(); button.props.onClick();
  await settle();
  assert.equal(calls.claims, 1); assert.equal(calls.reloads, 1);
  assert.deepEqual(calls.toasts, [["referral_config_claimed", undefined]]);
});

test("a refused claim says why", async () => {
  const { h, ReferralReward, calls } = setup(async () => { throw new Error("referral_config_not_enough"); });
  const [button] = nodes(h.render(ReferralReward, { config: config({ available: 1 }) }), n => n.type === "Button");
  button.props.onClick();
  await settle();
  assert.deepEqual(calls.toasts, [["err:referral_config_not_enough", "error"]]); assert.equal(calls.reloads, 0);
});

test("no reward set up shows nothing", () => {
  const { h, ReferralReward } = setup(() => assert.fail());
  assert.equal(h.render(ReferralReward, { config: config({ reward: null }) }), null);
});

for (const locale of ["fa", "en"]) test(`referral reward texts exist in ${locale}`, () => {
  const copy = JSON.parse(readFileSync(url(`locales/${locale}.json`), "utf8"));
  for (const key of ["referral_config_title", "referral_config_rule", "referral_config_rule_join", "referral_config_size",
    "referral_config_progress", "referral_config_available", "referral_config_claim", "referral_config_claimed",
    "invite_hint_config", "err_referral_config_not_enough", "err_referral_config_off", "err_referral_config_unavailable"]) {
    assert(copy[key]?.length > 0, key);
  }
});
