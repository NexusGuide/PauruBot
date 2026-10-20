import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, existsSync } from "node:fs";
import { screenHarness, nodes, deferred, tick } from "./helpers/wallet-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const wallet = readFileSync(url("screens/Wallet.tsx"), "utf8");
test("Wallet has no uncaught fire-and-forget history read", () => {
  assert.doesNotMatch(wallet, /api\.history\(\)\.then/);
});
test("History remounts on owner or confirmed gift revision, not another financial call", () => {
  assert.match(wallet, /<WalletHistory key=\{`\$\{me.id\}:\$\{historyVersion\}`\}/);
  assert.match(wallet, /setHistoryVersion\(value => value \+ 1\)/);
});
function setup(load) {
  const h = screenHarness();
  const { useResource } = h.compile(url("useResource.ts"), {});
  const { WalletHistory } = h.compile(url("screens/WalletHistory.tsx"), {
    "@shared/format": { money: n => String(n), dateTime: s => s },
    "../api": { api: { history: load } }, "../icons": { Icon: "Icon" },
    "../state": { useApp: () => ({ t: key => key, locale: "en" }) },
    "../ui": { ReadFailure: "ReadFailure", Spinner: "Spinner" }, "../useResource": { useResource },
  });
  return { ...h, render: () => h.render(WalletHistory) };
}
// Missing production implementation fails an explicit assertion, not a misleading fixture error.
test("history has a concrete implementation", () => assert(existsSync(url("screens/WalletHistory.tsx"))));
const behavior = existsSync(url("screens/WalletHistory.tsx")) ? test : test.skip;
behavior("a pending read displays loading, not an empty ledger", async () => {
  const p = deferred(), h = setup(() => p.promise); const tree = h.render();
  assert.equal(tree.props["aria-busy"], true); assert.equal(nodes(tree, n => n.props.role === "status").length, 1);
  assert.equal(nodes(tree, n => n.props.children === "no_transactions").length, 0);
  h.unmount(); p.resolve({ items: [] }); await tick();
});
behavior("a failed read exposes retry; retry is GET-only and can recover", async () => {
  let calls = 0; const failure = new Error("offline");
  const h = setup(async () => { calls++; if (calls === 1) throw failure; return { items: [] }; });
  h.render(); await tick(); let tree = h.render();
  const [error] = nodes(tree, n => n.type === "ReadFailure"); assert.equal(error.props.error, failure);
  assert.equal(nodes(tree, n => n.props.children === "no_transactions").length, 0);
  error.props.retry(); h.render(); await tick(); tree = h.render();
  assert.equal(calls, 2); assert.equal(nodes(tree, n => n.type === "ReadFailure").length, 0);
  assert.equal(nodes(tree, n => n.props.children === "no_transactions").length, 1); h.unmount();
});
behavior("successful rows retain transaction signs, ids, kind and timestamp", async () => {
  const h = setup(async () => ({ items: [
    { id: 9, amount: 250, kind: "topup", created_at: "2026-10-18T00:00:00Z" },
    { id: 8, amount: -80, kind: "charge", created_at: "2026-10-17T00:00:00Z" },
  ] })); h.render(); await tick(); const tree = h.render();
  assert.equal(nodes(tree, n => n.key === 9).length, 1); assert.equal(nodes(tree, n => n.key === 8).length, 1);
  assert.equal(nodes(tree, n => n.props.children === "tx_charge").length, 1);
  const signs = nodes(tree, n => n.props.dir === "ltr").map(n => n.props.children[0]); assert.deepEqual(signs, ["+", "−"]); h.unmount();
});
behavior("unmount ignores a late success or failure", async () => {
  for (const failed of [false, true]) {
    const p = deferred(), h = setup(() => p.promise); h.render(); h.unmount();
    if (failed) p.reject(new Error("late")); else p.resolve({ items: [] }); await tick();
    // The real useResource cleanup leaves the detached instance unchanged.
    const tree = h.render(); assert.equal(tree.props["aria-busy"], true);
  }
});
behavior("an obsolete read cannot replace a later retry result", async () => {
  const h = screenHarness(), old = deferred(); let calls = 0;
  const { useResource } = h.compile(url("useResource.ts"), {});
  const load = () => ++calls === 1 ? old.promise : Promise.resolve({ items: [] });
  const component = () => useResource(load);
  let result = h.render(component); result.retry(); h.render(component); await tick();
  result = h.render(component); assert.deepEqual(result.data, { items: [] });
  old.resolve({ items: ["obsolete"] }); await tick(); result = h.render(component);
  assert.deepEqual(result.data, { items: [] }); h.unmount();
});
