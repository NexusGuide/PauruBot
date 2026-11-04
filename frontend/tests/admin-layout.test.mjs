// The regrouped admin panel: hubs with tabs, settings categories and dependent options, and
// bot texts sorted into sections with a safe Telegram preview.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness } from "./helpers/rename-screen.mjs";

const src = (path) => new URL(`../src/admin/${path}`, import.meta.url);
const h = screenHarness();
const nav = h.compile(src("nav.ts"), {});
const settings = h.compile(src("settings-layout.ts"), {});
const texts = h.compile(src("texts-layout.ts"), {});
const locale = (name) => JSON.parse(readFileSync(src(`locales/${name}.json`), "utf8"));
const owner = nav.visibleFor(() => true, "owner");

test("every page of the panel is reachable from a hub", () => {
  const app = readFileSync(src("App.tsx"), "utf8");
  const routes = [...app.matchAll(/path="([^"]+)"/g)]
    .map((m) => m[1])
    .filter((path) => !path.includes(":") && path !== "*" && path !== "/account");
  const inHubs = new Set(nav.allPaths());
  assert.deepEqual(routes.filter((path) => !inHubs.has(path)), []);
  assert.equal(new Set(nav.allPaths()).size, nav.allPaths().length, "a page sits in one hub only");
});

test("the sidebar is short: hubs, not every page", () => {
  const groups = nav.visibleGroups(owner);
  const hubs = groups.flatMap((group) => group.hubs);
  assert.ok(hubs.length <= 13, `owner sidebar has ${hubs.length} entries`);
  assert.ok(nav.allPaths().length >= 25);
});

test("a page and its detail pages light up their hub", () => {
  const groups = nav.visibleGroups(owner);
  assert.equal(nav.hubFor(groups, "/customers/42").key, "nav_hub_customers");
  assert.equal(nav.hubFor(groups, "/services").key, "nav_hub_customers");
  assert.equal(nav.hubFor(groups, "/texts").key, "nav_hub_bot");
  assert.equal(nav.hubFor(groups, "/manual-sales").key, "nav_hub_products");
  assert.equal(nav.hubFor(groups, "/").key, "nav_inbox");
  assert.equal(nav.hubFor(groups, "/tickets/7").tabs.length, 1);
  assert.equal(nav.hubFor(groups, "/customersX"), undefined, "a prefix of another word is not a match");
});

test("tabs follow permissions: a hub shows only what this person may open", () => {
  const support = nav.visibleFor((p) => ["customers.read", "services.read", "settings.read"].includes(p), "support");
  const groups = nav.visibleGroups(support);
  const bot = nav.hubFor(groups, "/settings");
  assert.deepEqual(bot.tabs.map((tab) => tab.to), ["/settings", "/bot-menu", "/channels", "/appearance"]); // no texts.write
  assert.equal(nav.hubFor(groups, "/staff"), undefined, "no server hub without its rights");

  const reseller = nav.visibleFor(() => true, "reseller");
  const theirs = nav.visibleGroups(reseller);
  assert.deepEqual(nav.hubFor(theirs, "/services").tabs.map((tab) => tab.to), ["/services"]); // customers are shop-wide
  assert.equal(nav.hubFor(theirs, "/desk").key, "nav_hub_my_sales");
  assert.equal(nav.hubTarget(nav.hubFor(theirs, "/services")), "/services");
  assert.equal(nav.hubLabel(nav.hubFor(theirs, "/services")), "nav_services", "a one-page hub is named after its page");
  assert.equal(nav.hubLabel(nav.hubFor(nav.visibleGroups(owner), "/services")), "nav_hub_customers");
});

test("settings: groups gather into categories, an unknown group is not lost", () => {
  const cats = settings.categorize(["general", "payments", "crypto", "services", "trial", "growth", "brand_new"]);
  assert.deepEqual(cats.map((c) => c.key), ["general", "sales", "service", "growth", "other"]);
  assert.deepEqual(cats.find((c) => c.key === "other").groups, ["brand_new"]);
  assert.deepEqual(cats.find((c) => c.key === "sales").groups, ["payments", "crypto"]);
});

test("settings: an option shows only while the switches above it are on", () => {
  const keys = ["card_enabled", "card_accounts", "card_verify_enabled", "card_verify_min_amount", "tonpay_enabled",
    "tonpay_custom_enabled", "tonpay_custom_receipt_wait", "require_phone", "phone_country_codes", "custom_buy_enabled",
    "custom_panel_id", "bot_enabled", "min_topup"];
  const deps = settings.controllers(keys);
  assert.deepEqual(deps.card_verify_min_amount.sort(), ["card_enabled", "card_verify_enabled"]);
  assert.deepEqual(deps.card_verify_enabled, ["card_enabled"]);
  assert.deepEqual(deps.tonpay_custom_receipt_wait, ["tonpay_custom_enabled"], "custom Tonpay is its own gateway");
  assert.deepEqual(deps.phone_country_codes, ["require_phone"]);
  assert.deepEqual(deps.custom_panel_id, ["custom_buy_enabled"]);
  assert.equal(deps.min_topup, undefined);
  assert.equal(deps.bot_enabled, undefined);

  const values = { card_enabled: true, card_verify_enabled: false };
  const valueOf = (key) => values[key];
  assert.equal(settings.shown("card_accounts", deps, valueOf), true);
  assert.equal(settings.shown("card_verify_min_amount", deps, valueOf), false);
  values.card_verify_enabled = true; // an unsaved change counts at once
  assert.equal(settings.shown("card_verify_min_amount", deps, valueOf), true);
  values.card_enabled = false;
  assert.equal(settings.shown("card_verify_min_amount", deps, valueOf), false);
  assert.equal(settings.shown("min_topup", deps, valueOf), true);
});

test("settings search ignores case and Arabic letter forms", () => {
  assert.equal(settings.matchesQuery("كارت", "شماره کارت"), true);
  assert.equal(settings.matchesQuery("TOPUP", undefined, "min_topup"), true);
  assert.equal(settings.matchesQuery("", "anything"), true);
  assert.equal(settings.matchesQuery("zarinpal", "کارت", "card_enabled"), false);
});

test("texts: each goes to the part of the bot it belongs to", () => {
  assert.equal(texts.sectionOf("welcome"), "start");
  assert.equal(texts.sectionOf("btn_wallet"), "payment");
  assert.equal(texts.sectionOf("btn_buy"), "purchase");
  assert.equal(texts.sectionOf("btn_something_new"), "start");
  assert.equal(texts.sectionOf("adm_users_title"), "admin");
  assert.equal(texts.sectionOf("referral_config_ready"), "growth");
  assert.equal(texts.sectionOf("zzz_unknown"), "other");
  assert.equal(texts.sectionOf("all_configs_caption"), "services");
  assert.equal(texts.sectionOf("badge_hot"), "purchase");
  assert.deepEqual(
    ["status_x", "btn_trial", "start_gift", "welcome", "btn_buy"].sort(texts.bySectionOrder),
    ["welcome", "start_gift", "btn_buy", "btn_trial", "status_x"],
  );
  assert.equal(texts.isButton("btn_buy"), true);
  assert.equal(texts.isButton("acc_btn_login"), true);
  assert.equal(texts.isButton("welcome"), false);
  const counts = texts.countBySection(["welcome", "btn_buy", "adm_x", "adm_y"]);
  assert.equal(counts.admin, 2);
  assert.equal(Object.values(counts).reduce((a, b) => a + b, 0), 4);
});

test("texts: the real catalogue is mostly sorted, not dumped in other", () => {
  const keys = Object.keys(JSON.parse(readFileSync(new URL("../../paurubot/i18n/fa.json", import.meta.url), "utf8")));
  const other = keys.filter((key) => texts.sectionOf(key) === "other");
  assert.ok(other.length / keys.length < 0.12, `${other.length} of ${keys.length} in other`);
});

test("preview: Telegram's tags format, everything else stays literal text", () => {
  const nodes = texts.preview('سلام {name} <b>به <i>{shop}</i></b> &lt;3 <script>x</script> {code}', { name: "علی", shop: "S" });
  assert.deepEqual(nodes, [
    "سلام ", "علی", " ",
    { tag: "b", children: ["به ", { tag: "i", children: ["S"] }] },
    " <3 <script>x</script> ",
    { tag: "var", children: ["code"] },
  ]);
  assert.deepEqual(texts.preview("a</b>c"), ["a", "c"], "a stray closing tag is dropped");
  assert.deepEqual(texts.preview('<a href="https://x">t</a>'), [{ tag: "a", children: ["t"] }]);
  assert.deepEqual(texts.preview("<b>open"), [{ tag: "b", children: ["open"] }]);
});

test("every new label exists in both admin languages", () => {
  const fa = locale("fa"), en = locale("en");
  const groups = nav.visibleGroups(owner);
  const keys = [
    ...groups.map((group) => group.key),
    ...groups.flatMap((group) => group.hubs.flatMap((hub) => [hub.key, ...hub.tabs.map((tab) => tab.key)])),
    ...settings.CATEGORIES.map((category) => `settings_category_${category.key}`),
    ...texts.SECTIONS.map((section) => `texts_section_${section}`),
    "settings_unsaved", "settings_search", "texts_only_buttons", "text_preview", "text_insert_variable",
  ];
  for (const key of keys) {
    assert.ok(key in fa, `fa is missing ${key}`);
    assert.ok(key in en, `en is missing ${key}`);
  }
});

test("the Mini App look page edits real appearance settings, each with a label", () => {
  const system = readFileSync(src("screens/System.tsx"), "utf8");
  const keys = JSON.parse(system.match(/export const LOOK_KEYS = (\[[^\]]+\])/)[1]);
  const specs = readFileSync(new URL("../../paurubot/core/settings.py", import.meta.url), "utf8");
  const fa = locale("fa"), en = locale("en");
  assert.equal(keys.length, 4);
  for (const key of keys) {
    assert.match(specs, new RegExp(`"${key}": Spec\\([^)]*"appearance"\\)`), `${key} is an appearance setting`);
    assert.ok(`setting_${key}` in fa && `setting_${key}` in en, `label for ${key}`);
  }
  assert.ok(nav.allPaths().includes("/appearance"));
});
