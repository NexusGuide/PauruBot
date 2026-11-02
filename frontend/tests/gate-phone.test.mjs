// F-12: the Mini App saves the phone from Telegram's signed contact, and the gate never waits
// on the phone once it is saved, even when the terms are still to accept.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness, nodes } from "./helpers/rename-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
globalThis.window ??= {};
globalThis.window.setTimeout = fn => { fn(); return 0; };

function setup({ contact, share, gates = [] }) {
  const h = screenHarness(), calls = { share: [], gate: 0, checked: [], toasts: [], haptics: [] };
  const api = {
    sharePhone: async signed => { calls.share.push(signed); return share(signed); },
    gate: async () => { calls.gate++; return gates.shift() ?? { requirement: "phone_required" }; },
  };
  const tg = { requestContact: cb => cb(...contact), openTelegramLink() {}, close() {} };
  const { Gate } = h.compile(url("screens/Gate.tsx"), {
    "../api": { api },
    "../state": {
      useApp: () => ({ t: key => key, gate: { requirement: "phone_required", bot_username: "shop_bot" },
        checkGate: async next => { calls.checked.push(next); return next?.requirement === null; },
        toast: (text, tone) => calls.toasts.push([text, tone]) }),
      errorText: (_t, err) => `err:${err.message}`,
    },
    "../telegram": { haptic: kind => calls.haptics.push(kind), tg },
    "../ui": { Button: "Button", Screen: "Screen" },
  });
  const press = async () => {
    const [button] = nodes(h.render(Gate, {}), n => n.type === "Button" && n.props.children === "gate_phone_share");
    button.props.onClick();
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  };
  return { press, calls };
}

test("a signed contact is saved at once, without waiting on the bot", async () => {
  const { press, calls } = setup({ contact: [true, { status: "sent", response: "contact=x&auth_date=1&hash=y" }],
    share: () => ({ requirement: null }) });
  await press();
  assert.deepEqual(calls.share, ["contact=x&auth_date=1&hash=y"]);
  assert.deepEqual(calls.checked, [{ requirement: null }]);
  assert.equal(calls.gate, 0); assert.deepEqual(calls.toasts, []); assert.deepEqual(calls.haptics, ["success"]);
});

test("a saved phone with the terms still to accept moves on to the terms, with no false alarm", async () => {
  const terms = { requirement: "terms_required", terms: { version: 2, text: "..." } };
  const { press, calls } = setup({ contact: [true, { response: "signed" }], share: () => terms });
  await press();
  assert.deepEqual(calls.checked, [terms]); assert.equal(calls.gate, 0); assert.deepEqual(calls.toasts, []);
});

test("without a signed copy the bot path is awaited, and stops once the phone is no longer missing", async () => {
  const terms = { requirement: "terms_required" };
  const { press, calls } = setup({ contact: [true], share: () => assert.fail(),
    gates: [{ requirement: "phone_required" }, terms] });
  await press();
  assert.equal(calls.gate, 2); assert.deepEqual(calls.checked, [terms]);
  assert.deepEqual(calls.toasts, []); // before F-12 this showed "phone not received yet"
});

for (const code of ["phone_country_not_allowed", "phone_not_yours"]) test(`a refused number says why: ${code}`, async () => {
  const { press, calls } = setup({ contact: [true, { response: "signed" }], share: () => { throw new Error(code); } });
  await press();
  assert.deepEqual(calls.toasts, [[`err_${code}`, "error"]]); assert.equal(calls.gate, 0);
});

test("a signed copy the server cannot check falls back to the bot path", async () => {
  const { press, calls } = setup({ contact: [true, { response: "signed" }], share: () => { throw new Error("invalid_contact"); },
    gates: [{ requirement: null }] });
  await press();
  assert.equal(calls.gate, 1); assert.deepEqual(calls.checked, [{ requirement: null }]); assert.deepEqual(calls.toasts, []);
});

test("a cancelled share says so and reads nothing", async () => {
  const { press, calls } = setup({ contact: [false], share: () => assert.fail() });
  await press();
  assert.deepEqual(calls.toasts, [["gate_phone_cancelled", "error"]]); assert.equal(calls.gate, 0);
});

for (const locale of ["fa", "en"]) test(`phone errors are localized in ${locale}`, () => {
  const copy = JSON.parse(readFileSync(url(`locales/${locale}.json`), "utf8"));
  for (const key of ["err_phone_country_not_allowed", "err_phone_not_yours", "gate_phone_cancelled", "gate_phone_not_yet"]) assert(copy[key]?.length > 0, key);
});
