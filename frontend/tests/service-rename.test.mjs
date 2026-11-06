import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, existsSync } from "node:fs";
import { screenHarness, nodes, deferred, tick } from "./helpers/rename-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const source = readFileSync(url("screens/Services.tsx"), "utf8");
test("rename uses the existing endpoint inside the same local action gate", () => {
  assert.match(source, /api\.rename\(service.id, normalized\)/);
  assert.match(source, /const rename = \(\) => \{[\s\S]*?return runServiceAction\(gate.current/);
});
test("rename is an accessible form with draft preservation and no username mutation", () => {
  assert.match(source, /<form aria-label=\{t\("service_rename"\)\}/);
  assert.match(source, /htmlFor="service-label"/); assert.match(source, /aria-describedby="service-label-hint"/);
  assert.match(source, /maxLength=\{48\}/); assert.doesNotMatch(source, /service\.username\s*=/);
});
for (const lang of ["en", "fa"]) test(`rename copy is localized in ${lang}`, () => {
  const d = JSON.parse(readFileSync(url(`locales/${lang}.json`), "utf8"));
  for (const key of ["service_rename", "service_label", "service_rename_hint", "service_rename_save", "service_renamed"]) assert(d[key]?.length);
});
const implemented = existsSync(url("service-label.ts"));
test("rename label validation has a concrete implementation", () => assert(implemented));
const behavior = implemented ? test : test.skip;
behavior("labels trim whitespace, accept Unicode and reject blank or excessive code points", () => {
  const { normalizedServiceLabel: label } = screenHarness().compile(url("service-label.ts"), {});
  for (const value of ["", " \t\n", "x".repeat(49), "🙂".repeat(49)]) assert.equal(label(value), null);
  for (const value of ["برچسب فارسی", "x".repeat(48), "🙂".repeat(48)]) assert.equal(label(value), value);
  assert.equal(label("  مشتری اول  "), "مشتری اول");
});
function setup(rename) {
  const h = screenHarness(), calls = [], effects = [], reads = [];
  const original = { id: 7, label: "Original", username: "immutable_user", status: "active", is_trial: false,
    data_limit: 1000, used_bytes: 100, expire_at: null, panel_id: 1, subscription_url: "https://subscription.invalid/token", extras: null, changes: ["revoke"] };
  const api = { links: async id => { reads.push(id); return { links: [] }; }, service: async () => ({ service: original }), rename: (id, label) => { calls.push([id, label]); return rename(id, label); } };
  const { ServiceDetailBody } = h.compile(url("screens/Services.tsx"), {
    qrcode: { default: {} }, "react-router-dom": { useSearchParams: () => [new URLSearchParams()], useNavigate: () => () => {}, useParams: () => ({ id: "7" }) },
    "@shared/format": { date: () => "date", daysLeft: () => null, money: String, size: String },
    "../api": { api }, "../state": { useApp: () => ({ t: key => key, locale: "en", me: { extras: { upgrade: false } }, toast: msg => effects.push(msg) }), errorText: (_t, err) => err.message },
    "../telegram": { haptic: value => effects.push(value) }, "../icons": { Icon: "Icon" },
    "../ui": Object.fromEntries(["Button", "Chevron", "Empty", "Loading", "Panel", "ReadFailure", "Ring", "Row", "Screen"].map(x => [x, x])),
    "./Home": { timeRatio: () => 0.5, usageRatio: () => 0.5 },
    "./StockInventory": { StockInventory: "StockInventory" }, "./ServiceAccess": { ServiceAccess: "ServiceAccess" },
    "./Agency": { MyPanels: "MyPanels" }, "../data-left": h.compile(url("data-left.ts"), {}),
    "../useResource": { useResource: () => ({ data: { service: original }, error: null, loading: false }) },
    "../autoRenew/AutoRenew": { AutoRenew: "AutoRenew" }, "../usage/UsageChart": { UsageChart: "UsageChart" },
    "../service-pages": h.compile(url("service-pages.ts"), {}), "../service-read": h.compile(url("service-read.ts"), {}), "../service-label": h.compile(url("service-label.ts"), {}),
  }, "\nexport { ServiceDetailBody };\n");
  const render = () => h.render(ServiceDetailBody, { serviceId: 7 });
  const find = (tree, predicate) => { const [node] = nodes(tree, predicate); assert(node, "expected production control"); return node; };
  const form = tree => find(tree, n => n.type === "form");
  const input = tree => find(tree, n => n.type === "input" && n.props.id === "service-label");
  const open = () => { const tree = render(); find(tree, n => n.type === "Button" && n.props.children === "service_rename").props.onClick(); return render(); };
  const fill = text => { input(render()).props.onChange({ target: { value: text } }); return render(); };
  const submit = tree => form(tree).props.onSubmit({ preventDefault() {} });
  return { ...h, render, calls, effects, reads, find, form, input, open, fill, submit };
}
behavior("opening and cancelling rename makes no remote request", () => {
  const h = setup(() => assert.fail()); let tree = h.open(); assert.equal(h.input(tree).props.value, "Original");
  h.fill("Draft"); tree = h.render(); h.find(tree, n => n.type === "Button" && n.props.children === "cancel").props.onClick();
  assert.equal(nodes(h.render(), n => n.type === "form").length, 0); assert.equal(h.calls.length, 0); h.unmount();
});
behavior("blank, unchanged and programmatically overlong submits never call the API", () => {
  const h = setup(() => assert.fail()); h.open();
  for (const value of ["   ", "Original", "a".repeat(49)]) { h.submit(h.fill(value)); }
  assert.equal(h.calls.length, 0); h.unmount();
});
behavior("double submit sends one normalized request and displays only the server response", async () => {
  const p = deferred(), h = setup(() => p.promise); h.open(); const tree = h.fill("  New label  ");
  h.submit(tree); h.submit(tree); assert.deepEqual(h.calls, [[7, "New label"]]);
  const pending = h.render(); assert.equal(h.input(pending).props.disabled, true);
  for (const button of nodes(pending, n => n.type === "Button")) assert(button.props.disabled || button.props.busy);
  p.resolve({ service: { id: 7, label: "Confirmed", username: "immutable_user", used_bytes: 100, data_limit: 1000, status: "active", expire_at: null, extras: null } }); await tick();
  assert.equal(h.render().props.title, "Confirmed"); assert.equal(nodes(h.render(), n => n.type === "form").length, 0);
  assert.deepEqual(h.effects, ["success", "service_renamed"]); h.unmount();
});
behavior("failed rename preserves the draft and displays an explicit error; retry is deliberate", async () => {
  let attempt = 0; const h = setup(async () => { if (++attempt === 1) throw new Error("offline"); return { service: { id: 7, label: "Saved", username: "immutable_user", data_limit: 0, used_bytes: 0, expire_at: null } }; });
  h.open(); h.submit(h.fill("Draft")); await tick(); let tree = h.render();
  assert.equal(h.calls.length, 1); assert.equal(h.input(tree).props.value, "Draft"); assert.equal(h.render().props.title, "Original");
  assert.equal(h.find(tree, n => n.props.role === "alert").props.children, "offline");
  await tick(); assert.equal(h.calls.length, 1); h.submit(tree); await tick(); assert.equal(h.render().props.title, "Saved"); h.unmount();
});
for (const failed of [false, true]) behavior(`leaving during rename suppresses late ${failed ? "error" : "success"}`, async () => {
  const p = deferred(), h = setup(() => p.promise); h.open(); h.submit(h.fill("Draft")); h.unmount();
  if (failed) p.reject(new Error("late")); else p.resolve({ service: { label: "Late" } }); await tick();
  assert.deepEqual(h.effects, []); assert.equal(h.calls.length, 1);
});
behavior("the same gate prevents a config read from overlapping rename", async () => {
  const p = deferred(), h = setup(() => p.promise); h.open(); h.submit(h.fill("Draft"));
  // Revoke uses the same gate; a queued browser callback cannot start another action.
  const busy = h.render(); assert.equal(h.input(busy).props.disabled, true);
  h.find(busy, n => n.type === "Button" && n.props.children === "show_configs").props.onClick();
  assert.deepEqual(h.reads, []); assert.equal(h.calls.length, 1); h.unmount(); p.reject(new Error("late")); await tick();
});

behavior("service-linked support is available when idle and disabled during rename", async () => {
  const p = deferred(), h = setup(() => p.promise);
  const support = tree => h.find(tree, n => n.type === "Button" && n.props.children === "tickets_service_report");
  assert.equal(support(h.render()).props.disabled, false);
  h.open(); h.submit(h.fill("Draft"));
  assert.equal(support(h.render()).props.disabled, true);
  h.unmount(); p.reject(new Error("late")); await tick();
});
