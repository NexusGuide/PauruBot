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
    .filter((path) => !path.includes(":") && path !== "*" && path !== "/account" && path !== "/more");
  const inHubs = new Set(nav.allPaths());
  assert.deepEqual(routes.filter((path) => !inHubs.has(path)), []);
  assert.equal(new Set(nav.allPaths()).size, nav.allPaths().length, "a page sits in one hub only");
});

test("the sidebar is short: a few folders, each with a few entries", () => {
  const groups = nav.visibleGroups(owner);
  assert.ok(groups.length <= 7, `owner sidebar has ${groups.length} groups`);
  for (const group of groups) assert.ok(group.hubs.length <= 5, `${group.key} has ${group.hubs.length} entries`);
  assert.ok(nav.allPaths().length >= 25);
  assert.equal(nav.groupFor(groups, "/customer-bots").key, "nav_group_resellers");
  assert.equal(nav.groupFor(groups, "/variables").key, "nav_group_settings");
  assert.equal(nav.groupFor(groups, "/nowhere"), undefined);
});

test("the phone bar names real hubs, and waiting counts add up per hub", () => {
  const groups = nav.visibleGroups(owner);
  const hubs = groups.flatMap((group) => group.hubs);
  for (const tab of nav.MOBILE_TABS) assert.ok(hubs.some((hub) => hub.key === tab.hub), tab.hub);
  const sales = nav.hubFor(groups, "/payments");
  assert.equal(nav.hubCount(sales, { pending_receipts: 3, open_tickets: 5 }), 3);
  assert.equal(nav.hubCount(nav.hubFor(groups, "/tickets"), { pending_receipts: 3, open_tickets: 5 }), 5);
  assert.equal(nav.hubCount(nav.hubFor(groups, "/catalog"), { pending_receipts: 3 }), 0);
});

test("the sidebar opens one folder at a time", () => {
  assert.equal(nav.nextOpen(null, "nav_group_shop"), "nav_group_shop");
  assert.equal(nav.nextOpen("nav_group_shop", "nav_group_marketing"), "nav_group_marketing", "the other one closes");
  assert.equal(nav.nextOpen("nav_group_shop", "nav_group_shop"), null, "clicking the open one closes it");
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
  assert.deepEqual(bot.tabs.map((tab) => tab.to), ["/settings", "/bot-menu", "/button-looks", "/appearance", "/quick-links", "/variables", "/channels"]); // no texts.write
  assert.equal(nav.hubFor(groups, "/customer-bots"), undefined, "customer bots need resellers.manage");
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
  assert.deepEqual(cats.map((c) => c.key), ["general", "sales", "pay", "growth", "other"]);
  assert.deepEqual(cats.find((c) => c.key === "other").groups, ["brand_new"]);
  assert.deepEqual(cats.find((c) => c.key === "pay").groups, ["payments", "crypto"]);
  assert.deepEqual(cats.find((c) => c.key === "sales").groups, ["services", "trial"]);
  assert.deepEqual(settings.categorize(["reports"]).map((c) => c.key), ["alerts"]);
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
    ...Object.keys(JSON.parse(readFileSync(src("locales/fa.json"), "utf8"))).filter((key) => key.startsWith("cbots_") || key.startsWith("qlinks_") || key.startsWith("vars_")),
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

test("customer bots: filters and search", () => {
  const screen = h.compile(src("screens/CustomerBots.tsx"), {
    "react-router-dom": {}, "@shared/format": {}, "../api": {}, "../hooks": {}, "../session": {}, "../ui": {},
  });
  const bot = (shop_id, kind, state, title, owner) => ({ shop_id, kind, state, title, bot_username: `${title}_bot`, owner });
  const items = [
    bot(1, "own", "selling", "Ali", { id: 1, name: "علی", username: "ali" }),
    bot(2, "reseller", "blocked", "Sara", { id: 2, name: "سارا", username: null }),
    bot(3, "reseller", "selling", "Mina", null),
  ];
  const ids = (list) => list.map((b) => b.shop_id);
  assert.deepEqual(ids(screen.filterBots(items, "all", "")), [1, 2, 3]);
  assert.deepEqual(ids(screen.filterBots(items, "reseller", "")), [2, 3]);
  assert.deepEqual(ids(screen.filterBots(items, "own", "")), [1]);
  assert.deepEqual(ids(screen.filterBots(items, "attention", "")), [2]);
  assert.deepEqual(ids(screen.filterBots(items, "all", "@sara_BOT")), [2]);
  assert.deepEqual(ids(screen.filterBots(items, "all", "علی")), [1]);
});

test("quick links and variables", () => {
  const screen = h.compile(src("screens/BotLinks.tsx"), {
    "react-router-dom": {}, "@shared/format": {}, "../hooks": {}, "../session": {}, "../ui": {}, "../texts-layout": texts,
  });
  assert.equal(screen.linkFor("pauru_bot", "go_buy"), "https://t.me/pauru_bot?start=go_buy");
  assert.equal(screen.linkFor(null, "go_buy"), "?start=go_buy");
  assert.equal(screen.variableSection(["btn_wallet", "wallet_topup", "welcome"]), texts.sectionOf("btn_wallet"));
  assert.equal(screen.variableSection([]), "other");
  const app = readFileSync(src("App.tsx"), "utf8");
  for (const path of ["/customer-bots", "/quick-links", "/variables"]) assert.ok(app.includes(`path="${path}"`), path);
});

test("settings editors match the server's lists and store what is off", () => {
  const editors = h.compile(src("settings-editors.tsx"), { "./session": {}, "./ui": {} });
  const specs = readFileSync(new URL("../../paurubot/core/settings.py", import.meta.url), "utf8");
  const tuple = (name) => [...specs.match(new RegExp(`${name} = \\(([^)]*)\\)`))[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...editors.REPORT_TOPICS], tuple("REPORT_TOPICS"));
  assert.deepEqual([...editors.STAFF_EVENTS], tuple("STAFF_EVENTS"));
  assert.deepEqual([...editors.DAILY_SECTIONS], tuple("DAILY_SECTIONS"));
  assert.deepEqual(editors.setMuted([], "dm:panel", false), ["dm:panel"]);
  assert.deepEqual(editors.setMuted(["dm:panel", "group:orders"], "dm:panel", true), ["group:orders"]);
  assert.deepEqual(editors.parseWords(" کند، قطع ,slow,, "), ["کند", "قطع", "slow"]);
  const fa = locale("fa"), en = locale("en");
  for (const key of [...editors.STAFF_EVENTS.map((e) => `alert_event_${e}`), ...editors.REPORT_TOPICS.map((t) => `log_topic_${t}`),
    ...editors.DAILY_SECTIONS.map((s) => `daily_section_${s}`)]) {
    assert.ok(key in fa && key in en, key);
  }
  for (const key of ["max_active_services", "max_daily_purchases", "alerts_quiet_start", "alerts_quiet_end", "daily_report_enabled",
    "daily_report_sections", "alerts_muted", "auto_replies", "terms_when"]) {
    assert.ok(`setting_${key}` in fa && `setting_${key}` in en, key);
  }
});

test("settings nav: two levels like the design, every settings group reachable, the right tab lit", () => {
  const sn = h.compile(src("settings-nav.ts"), {});
  assert.deepEqual(sn.SETTINGS_NAV.map((g) => g.key), ["look", "sales", "pay", "growth", "alerts", "connect"]);
  const specs = readFileSync(new URL("../../paurubot/core/settings.py", import.meta.url), "utf8");
  const serverGroups = new Set([...specs.matchAll(/Spec\([^)]*"([a-z_]+)"\)/g)].map((m) => m[1]));
  const elsewhere = ["appearance", "resale", "backups", "own"]; // own screens, or never shown
  const missing = [...serverGroups].filter((g) => !elsewhere.includes(g) && !sn.navGroups().includes(g));
  assert.deepEqual(missing, [], "every settings group has a tab");
  assert.equal(sn.activeSettings("/texts", "").tab.key, "messages", "no kind: message texts");
  assert.equal(sn.activeSettings("/texts", "?kind=buttons").tab.key, "buttons");
  assert.equal(sn.activeSettings("/settings", "?group=crypto").group.key, "pay");
  assert.equal(sn.activeSettings("/settings", "").tab.key, "general");
  assert.equal(sn.activeSettings("/channels", "").group.key, "connect");
  assert.equal(sn.activeSettings("/customers", ""), undefined);
  const support = sn.visibleSettings((p) => p === "settings.read");
  assert.deepEqual(support.find((g) => g.key === "look").tabs.map((tab) => tab.key), ["menu", "miniapp", "general"]);
  const fa = locale("fa"), en = locale("en");
  for (const g of sn.SETTINGS_NAV) {
    for (const key of [`settings_nav_${g.key}`, ...g.tabs.map((tab) => `settings_tab_${tab.key}`)]) {
      assert.ok(key in fa && key in en, key);
    }
  }
  // Every page of the nav is a page of the settings hub, so the nav shows on it.
  for (const tab of sn.SETTINGS_NAV.flatMap((g) => g.tabs)) {
    assert.equal(nav.hubFor(nav.visibleGroups(owner), tab.to.split("?")[0]).key, "nav_hub_bot", tab.to);
  }
});

test("texts: messages and button labels are separate tabs", () => {
  assert.equal(texts.kindOf("btn_buy"), "buttons");
  assert.equal(texts.kindOf("acc_btn_login"), "buttons");
  assert.equal(texts.kindOf("welcome"), "messages");
});

test("staff tools: the same limits and audiences as the server", () => {
  const tools = h.compile(src("screens/Tools.tsx"), {
    "@shared/format": {}, "../api": {}, "../hooks": {}, "../session": {}, "../ui": {},
  });
  const server = readFileSync(new URL("../../paurubot/api/routes/admin/tools.py", import.meta.url), "utf8");
  const audiences = [...server.match(/Audience = Literal\[([^\]]+)\]/)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...tools.AUDIENCES], audiences);
  assert.equal(tools.GIFT_MIN, 1000);
  assert.equal(tools.GIFT_MAX, 10_000_000);
  assert.equal(tools.validPercent(10), true);
  assert.equal(tools.validPercent(-90), true);
  assert.equal(tools.validPercent(0), false);
  assert.equal(tools.validPercent(91), false);
  assert.equal(tools.validPercent(null), false);
  assert.equal(tools.priceAfter(100_000, 10), 110_000);
  assert.equal(tools.priceAfter(123_456, -15), 104_900);
  const fa = locale("fa"), en = locale("en");
  for (const a of tools.AUDIENCES) assert.ok(`audience_${a}` in fa && `audience_${a}` in en, a);
});

test("ClockBot parity tools: inputs the server accepts", () => {
  const cb = h.compile(src("screens/ClockTools.tsx"), {
    "react-router-dom": {}, "@shared/format": {}, "../api": {}, "../hooks": {}, "../session": {}, "../ui": {},
  });
  assert.equal(cb.validTelegramId(771100223), true);
  assert.equal(cb.validTelegramId(12), false);
  assert.equal(cb.validTelegramId(null), false);
  assert.deepEqual(cb.parseAmounts("50000، 100000 abc 0 -5,200000"), [50000, 100000, 200000]);
  assert.equal(cb.parseAmounts("1 2 3 4 5 6 7 8 9 10").length, 8);
  const growth = readFileSync(new URL("../../paurubot/api/routes/admin/growth.py", import.meta.url), "utf8");
  const finished = [...growth.match(/FINISHED_BROADCAST = \[([^\]]+)\]/)[1].matchAll(/JobStatus\.([A-Z]+)/g)].map((m) => m[1].toLowerCase());
  assert.deepEqual([...cb.FINISHED_BROADCAST].sort(), finished.sort());
  assert.ok(nav.allPaths().includes("/trial-users"));
});

test("ClockBot parity batch 2: editor inputs the server accepts", () => {
  const cb = h.compile(src("screens/ClockTools2.tsx"), {
    react: { useState: () => [null, () => {}] }, "../api": {}, "../hooks": {}, "../session": {}, "../ui": {},
  });
  assert.deepEqual(
    cb.cleanTiers([{ min: 10, percent: 15 }, { min: 1, percent: 5 }, { min: 5, percent: 10 }, { min: 3, percent: 95 }]),
    [{ min: 5, percent: 10 }, { min: 10, percent: 15 }],
  );
  assert.equal(cb.toIso(""), "");
  assert.equal(cb.toIso("not a date"), "");
  assert.equal(cb.toLocal(cb.toIso("2026-11-12T20:00")), "2026-11-12T20:00");
  assert.deepEqual(cb.parseChatIds("-100123، 456 abc 0 7.5"), [-100123, 456]);
  assert.equal(cb.parseChatIds(Array.from({ length: 12 }, (_, i) => i + 1).join(",")).length, 10);
  assert.ok(nav.allPaths().includes("/stuck-orders"));
  const fa = locale("fa"), en = locale("en");
  const screen = readFileSync(src("screens/ClockTools2.tsx"), "utf8");
  const used = new Set([...screen.matchAll(/\bt\(\s*"([a-z0-9_]+)"/g)].map((m) => m[1]));
  for (const m of screen.matchAll(/t\(\w+ \? "([a-z0-9_]+)" : "([a-z0-9_]+)"\)/g)) used.add(m[1]).add(m[2]);
  assert.ok(used.size > 30);
  for (const key of used) assert.ok(key in fa && key in en, key);
});

test("Foxima parity F1: payment method rules editor matches the server", () => {
  const fx = h.compile(src("screens/FoximaTools.tsx"), { "../session": {}, "../ui": {} });
  const engine = readFileSync(new URL("../../paurubot/payments/engine.py", import.meta.url), "utf8");
  assert.ok(engine.includes("def method_rule"));
  assert.deepEqual(
    fx.cleanRules([
      { provider: "zarinpal", min: 100, max: 50, help: "" },
      { provider: "card", min: 0, max: 0, help: "ok" },
      { provider: "card", min: 1, max: 2, help: "" },
      { provider: "nope", min: 0, max: 0, help: "" },
    ]),
    [{ provider: "card", min: 0, max: 0, help: "ok" }],
  );
  const fa = locale("fa"), en = locale("en");
  for (const m of fx.METHODS) assert.ok(`provider_${m}` in fa && `provider_${m}` in en, m);
  for (const k of ["setting_payment_method_rules", "setting_referral_require_phone", "setting_referral_min_account_hours", "method_rule_add"]) assert.ok(k in fa && k in en, k);
});

test("ClockBot parity: button looks page saves only real looks", () => {
  const bl = h.compile(src("screens/ButtonLooks.tsx"), { react: { useState: () => [null, () => {}], useEffect: () => {}, useMemo: (f) => f() }, "../api": {}, "../hooks": {}, "../session": {}, "../texts-layout": { SECTIONS: [], sectionOf: () => "other" }, "../ui": {} });
  assert.deepEqual(bl.cleanLooks({ btn_buy: { style: "success", emoji_id: "53683241706" }, btn_back: { style: "", emoji_id: "" }, btn_x: { style: "pink", emoji_id: "abc" } }), { btn_buy: { style: "success", emoji_id: "53683241706" } });
  assert.ok(nav.allPaths().includes("/button-looks"));
  const fa = locale("fa"), en = locale("en");
  for (const tone of ["default", "primary", "success", "danger"]) assert.ok(`blooks_tone_${tone}` in fa && `blooks_tone_${tone}` in en);
});
