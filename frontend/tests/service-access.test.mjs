// F-11: last online, the app that last fetched the subscription, and live devices on request.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { screenHarness, nodes, deferred, tick } from "./helpers/rename-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const text = tree => nodes(tree, () => true).flatMap(n => [n.props.children].flat()).filter(v => typeof v === "string").join(" ");

function setup(read) {
  const h = screenHarness(), reads = [];
  const api = { serviceDevices: id => { reads.push(id); return read(id); } };
  const { ServiceAccess } = h.compile(url("screens/ServiceAccess.tsx"), {
    "@shared/format": { dateTime: iso => `at:${iso}`, digits: String },
    "../api": { api },
    "../state": { useApp: () => ({ t: (key, vars) => vars ? `${key}:${JSON.stringify(vars)}` : key, locale: "en" }), errorText: (_t, err) => `err:${err.message}` },
    "../ui": { Button: "Button", Panel: "Panel" },
  });
  const button = tree => nodes(tree, n => n.type === "Button")[0];
  return { ...h, ServiceAccess, reads, button };
}

const access = { online_at: "2026-11-02T10:00:00+00:00", client: "v2rayNG 1.9.1", client_at: "2026-11-02T09:00:00+00:00", devices: true };

test("shows last online and the app without any remote read", () => {
  const h = setup(() => assert.fail());
  const tree = h.render(h.ServiceAccess, { serviceId: 7, access });
  const shown = text(tree);
  assert.match(shown, /at:2026-11-02T10:00:00\+00:00/); assert.match(shown, /v2rayNG 1\.9\.1/);
  assert.equal(h.reads.length, 0); assert.equal(h.button(tree).props.children, "access_devices_show");
});

test("unknown values read as such, and no devices button where the panel cannot list them", () => {
  const h = setup(() => assert.fail());
  const tree = h.render(h.ServiceAccess, { serviceId: 7, access: { online_at: null, client: null, client_at: null, devices: false } });
  const shown = text(tree);
  assert.match(shown, /access_never/); assert.match(shown, /access_unknown/); assert.doesNotMatch(shown, /access_client_at/);
  assert.equal(h.button(tree), undefined);
  assert.equal(h.render(h.ServiceAccess, { serviceId: 7, access: undefined }), null);
});

test("devices are read on request, once at a time, and shown as the server masked them", async () => {
  const p = deferred(), h = setup(() => p.promise);
  h.button(h.render(h.ServiceAccess, { serviceId: 7, access })).props.onClick();
  h.button(h.render(h.ServiceAccess, { serviceId: 7, access })).props.onClick();
  assert.deepEqual(h.reads, [7]);
  p.resolve({ count: 2, limit: 2, addresses: ["185.12.33.x", "2001:0db8:0001:…"] }); await tick();
  const tree = h.render(h.ServiceAccess, { serviceId: 7, access });
  const shown = text(tree);
  assert.match(shown, /access_devices_of:\{"count":"2","limit":"2"\}/);
  assert.deepEqual(nodes(tree, n => n.type === "li").map(n => n.props.children), ["185.12.33.x", "2001:0db8:0001:…"]);
  assert.equal(h.button(tree).props.children, "access_devices_refresh");
});

test("a failed read shows the error and can be retried", async () => {
  let attempt = 0;
  const h = setup(async () => { if (++attempt === 1) throw new Error("rate_limited"); return { count: 0, limit: 0, addresses: [] }; });
  h.button(h.render(h.ServiceAccess, { serviceId: 7, access })).props.onClick(); await tick();
  assert.match(text(h.render(h.ServiceAccess, { serviceId: 7, access })), /err:rate_limited/);
  h.button(h.render(h.ServiceAccess, { serviceId: 7, access })).props.onClick(); await tick();
  const shown = text(h.render(h.ServiceAccess, { serviceId: 7, access }));
  assert.doesNotMatch(shown, /err:/); assert.match(shown, /access_devices_none/);
});

for (const locale of ["fa", "en"]) test(`access texts exist in ${locale}`, () => {
  const copy = JSON.parse(readFileSync(url(`locales/${locale}.json`), "utf8"));
  for (const key of ["access_title", "access_online", "access_never", "access_client", "access_unknown", "access_client_at",
    "access_devices_show", "access_devices_refresh", "access_devices", "access_devices_of", "access_devices_none",
    "access_devices_hint", "err_devices_unsupported", "err_panel_unavailable", "err_rate_limited"]) assert(copy[key]?.length > 0, key);
});
