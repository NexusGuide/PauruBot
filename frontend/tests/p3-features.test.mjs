// F-32 formats, F-33 own discounts and checkout hints, F-22 ticket reactions.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness, nodes } from "./helpers/rename-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const read = path => readFileSync(url(path), "utf8");
const text = tree => nodes(tree, () => true).flatMap(n => [n.props.children].flat()).filter(v => typeof v === "string").join(" ");
const tick = () => new Promise(resolve => setImmediate(resolve));

function myDiscounts(data) {
  const h = screenHarness(), copied = [];
  const { MyDiscounts } = h.compile(url("screens/MyDiscounts.tsx"), {
    "@shared/format": { date: iso => `d:${iso}`, money: String },
    "../api": { api: { myDiscounts: async () => data } },
    "../state": { useApp: () => ({ t: (k, v) => v ? `${k}:${JSON.stringify(v)}` : k, locale: "en", toast: m => copied.push(m) }) },
    "../ui": { Panel: "Panel" },
  });
  return { h, MyDiscounts, copied };
}

test("own discounts: tier, next tier and codes with their conditions", async () => {
  const { h, MyDiscounts } = myDiscounts({
    standing_percent: 5, reseller: false, tier: { title: "Silver", percent: 5 },
    next_tier: { title: "Gold", percent: 10, remaining: 350000 },
    codes: [{ code: "MINE10", kind: "percent", value: 10, max_discount: null, min_amount: 200000, expires_at: "2026-12-01T00:00:00Z",
      first_purchase_only: true, sticky: false, fits: null, reason: null, saving: null }],
  });
  h.render(MyDiscounts, {}); await tick();
  const shown = text(h.render(MyDiscounts, {}));
  assert.match(shown, /my_discounts_tier/); assert.match(shown, /my_discounts_next/); assert.match(shown, /MINE10/);
  assert.match(shown, /my_codes_first_only/); assert.match(shown, /my_codes_min/); assert.match(shown, /my_codes_until/);
});

test("own discounts: nothing to show renders nothing", async () => {
  const { h, MyDiscounts } = myDiscounts({ standing_percent: 0, reseller: false, tier: null, next_tier: null, codes: [] });
  h.render(MyDiscounts, {}); await tick();
  assert.equal(h.render(MyDiscounts, {}), null);
});

test("checkout offers only fitting codes and applies the chosen one", () => {
  const source = read("screens/Checkout.tsx");
  assert.match(source, /api\.myDiscounts\(orderId\)/);
  assert.match(source, /c\.fits && \(c\.saving \?\? 0\) > 0/);
  assert.match(source, /applyCode = async \(value: string = code\)/);
});

test("formats are offered from the server's list, never built in the app", () => {
  const source = read("screens/Services.tsx");
  assert.match(source, /service\.sub_formats\?\.map/); assert.match(source, /copy\(item\.url\)/);
  assert.doesNotMatch(source, /\?format=/);
});

test("a support reply can be rated and the rating taken back", () => {
  const source = read("screens/Tickets.tsx");
  assert.match(source, /message\.from === "support" && <Reaction/);
  assert.match(source, /const wanted = value === next \? null : next/);
  assert.match(source, /api\.reactTicket\(ticketId, messageId, wanted\)/);
});

for (const locale of ["fa", "en"]) test(`new texts exist in ${locale}`, () => {
  const copy = JSON.parse(read(`locales/${locale}.json`));
  for (const key of ["sub_formats", "sub_formats_hint", "sub_format_v2ray", "sub_format_clash", "sub_format_clash-meta", "sub_format_sing-box", "sub_format_xray-json",
    "my_discounts_title", "my_discounts_tier", "my_discounts_reseller", "my_discounts_next", "my_codes_until", "my_codes_sticky", "my_codes_first_only",
    "my_codes_min", "my_codes_hint", "my_codes_fit", "my_codes_saves", "tickets_rate", "tickets_rate_up", "tickets_rate_down"]) assert(copy[key]?.length > 0, key);
});
