import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
import {readFileSync} from "node:fs";
import test, {mock, after} from "node:test";
import {screenHarness} from "./helpers/wallet-screen.mjs";
const root = process.env.ALERT_SOURCE_ROOT ? pathToFileURL(process.env.ALERT_SOURCE_ROOT + "/src/app/") : new URL("../src/app/", import.meta.url);
const harness = screenHarness();
const format = harness.compile(new URL("../shared/format.ts", root), {});
const alerts = harness.compile(new URL("alerts.ts", root), {"@shared/format": format});
const NOW = Date.parse("2026-10-18T00:00:00Z");
mock.method(Date, "now", () => NOW); after(() => mock.restoreAll());
const t = (key, params) => key === "units" ? "B,KB,MB,GB,TB" : key + (params ? JSON.stringify(params) : "");
const service = (patch = {}) => ({id:7,label:"Synthetic",username:"fixed_7",status:"active",data_limit:100,
 used_bytes:88,expire_at:new Date(NOW + 2.5 * 86400000).toISOString(),...patch});
const policy = (percent=10, days=2) => ({warn_remaining_percent:percent,warn_days:days});
const build = (items, p, history=[]) => alerts.buildAlerts(items, history, t, "en", p);
// Alert ids carry their service period after "@" (SEC-F28); these cases compare the kind.
const ids = items => items.map(x => x.id.split("@")[0]);
const cases = JSON.parse(readFileSync(new URL("../../tests/fixtures/alert_policy_cases.json", import.meta.url), "utf8"));
for (const item of cases) test(`shared threshold case: ${item.name}`, () => {
 assert.equal(typeof alerts.lowVolumeWarning, "function", "shared policy helper is implemented");
 const s = service({status:item.status,data_limit:item.data_limit,used_bytes:item.data_limit-item.remaining,
  expire_at:item.seconds===null ? null : new Date(NOW + item.seconds*1000).toISOString()});
 assert.equal(alerts.lowVolumeWarning(s,item.policy),item.low_volume);
 assert.equal(ids(build([s],item.policy)).includes("s7-expiring"),item.expiring);
});
test("shop policy replaces old 15 percent and 3 day literals", () => {
 assert.deepEqual(build([service()],policy()),[]);
 assert.deepEqual(ids(build([service()],policy(20,4))),["s7-low","s7-expiring"]);
});
test("missing policy suppresses predictive warnings but preserves terminal and wallet records", () => {
 const items=build([service(),service({id:8,status:"limited",used_bytes:100,expire_at:null}),service({id:9,expire_at:new Date(NOW-1).toISOString()})],null,
 [{id:33,amount:100,balance_after:100,kind:"topup",created_at:new Date(NOW).toISOString()}]);
 assert.deepEqual(ids(items),["s8-data","s9-expired","w33"]);
 assert.equal(items.find(x=>x.id==="w33").href,"/wallet");
});
test("expiry warning uses seconds, inclusive threshold and the existing worker day label", () => {
 const s=service({used_bytes:0,expire_at:new Date(NOW+2*86400000).toISOString()});
 assert.match(build([s],policy())[0].body,/"days":"3"/);
 assert.deepEqual(build([{...s,expire_at:new Date(NOW+2*86400000+1).toISOString()}],policy()),[]);
});
test("invalid or unlimited expiry cannot fabricate a warning", () => {
 for(const value of [null,"not-a-date"])assert.deepEqual(build([service({used_bytes:0,expire_at:value})],policy()),[]);
});
test("deleted services never create a feed item", () => assert.deepEqual(build([service({status:"deleted",used_bytes:100})],policy(100,100)),[]));
for (const [name,value] of [["missing",undefined],["null",null],["array",[]],["string","10"],["incomplete",{warn_days:2}],
 ["numeric string",{warn_days:"2",warn_remaining_percent:10}],["boolean",{warn_days:2,warn_remaining_percent:true}],
 ["infinite",{warn_days:Infinity,warn_remaining_percent:10}],["fractional",{warn_days:1.5,warn_remaining_percent:10}],
 ["unsafe integer",{warn_days:Number.MAX_SAFE_INTEGER+1,warn_remaining_percent:10}]]) {
 test(`malformed/old policy fails closed: ${name}`,()=>{
  assert.equal(typeof alerts.parseAlertPolicy,"function","policy parser is implemented");
  assert.equal(alerts.parseAlertPolicy(value),null);
 });
}
test("valid zero, negative and custom thresholds are not silently clamped to new policy",()=>{
 assert.equal(typeof alerts.parseAlertPolicy,"function");
 for(const value of [policy(),policy(0,0),policy(-1,-2),policy(120,40)])assert.equal(alerts.parseAlertPolicy(value),value);
});
test("both feed callers pass policy and remount their read subtree by authenticated customer",()=>{
 for(const [file,body] of [["Home","HomeBody"],["Notifications","NotificationFeed"]]){
  const source=readFileSync(new URL(`screens/${file}.tsx`,root),"utf8");
  assert.match(source,new RegExp(`<${body} key=\\{me.id\\}`));
  assert.match(source,/parseAlertPolicy\(me\??\.alert_policy\)/);
  assert.match(source,/buildAlerts\([^;]*locale, policy\)/);
 }
 const source=readFileSync(new URL("screens/Notifications.tsx",root),"utf8");
 assert.match(source,/alerts_policy_unavailable/);
});

// Integration regression: alert policy must preserve payment-intent translations.
for (const locale of ["en", "fa"]) test(`alert policy preserves ${locale} payment-intent messages`, () => {
 const messages = JSON.parse(readFileSync(new URL(`../src/app/locales/${locale}.json`, import.meta.url), "utf8"));
 for (const key of ["err_payment_intent_conflict", "err_payment_intent_closed", "err_payment_intent_invalid"]) {
  assert.equal(typeof messages[key], "string", key); assert(messages[key].trim().length > 0, key);
 }
});

// SEC-F28: read state follows the service period and stays with one customer of one shop.
test("a new service period makes the same kind of alert unread again", () => {
 const exhausted = service({status:"limited",used_bytes:100});
 const [first] = build([exhausted],policy());
 assert.equal(build([exhausted],policy())[0].id, first.id, "same period, same id: stays read");
 const renewed = {...exhausted, expire_at:new Date(NOW + 32 * 86400000).toISOString()};
 assert.notEqual(build([renewed],policy())[0].id, first.id, "renewal starts a new period");
 const topped = {...exhausted, data_limit:200, used_bytes:200};
 assert.notEqual(build([topped],policy())[0].id, first.id, "an add-on starts a new period");
 const stamped = {...exhausted, limited_at:"2026-10-17T10:00:00Z"};
 const again = {...exhausted, limited_at:"2026-10-17T22:00:00Z"};
 assert.notEqual(build([stamped],policy())[0].id, build([again],policy())[0].id, "data ran out again: new alert");
});
test("read state is stored per customer, never in the shared legacy key", () => {
 const store = new Map([["pauru.alerts.read", JSON.stringify(["s7-data"])]]);
 // Newer Node has a built-in, read-only Web Storage global: replace it, don't assign over it.
 Object.defineProperty(globalThis, "localStorage", { configurable: true, writable: true, value: {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
 } });
 try {
  assert.equal(alerts.readAlertIds(11).size, 0, "the old shared list is not inherited");
  alerts.markAlertsRead(11, ["s7-data@x"]);
  assert.deepEqual([...alerts.readAlertIds(11)], ["s7-data@x"]);
  assert.equal(alerts.readAlertIds(12).size, 0, "another customer (account or shop) keeps its own");
  assert.equal(store.has("pauru.alerts.read"), false, "the shared legacy key is removed");
 } finally {
  delete globalThis.localStorage;
 }
});
test("warn_time_percent triggers expiring warning when warn_days is 0 and fails closed when invalid", () => {
  assert.equal(alerts.parseAlertPolicy({warn_days: 0, warn_remaining_percent: 0, warn_time_percent: "20"}), null);
  const p = {warn_days: 0, warn_remaining_percent: 0, warn_time_percent: 20};
  assert.equal(alerts.parseAlertPolicy(p), p);
  const sWarn = service({used_bytes: 0, plan_days: 30, expire_at: new Date(NOW + 5 * 86400000).toISOString()});
  const sOk = service({used_bytes: 0, plan_days: 30, expire_at: new Date(NOW + 10 * 86400000).toISOString()});
  assert.deepEqual(ids(build([sWarn], p)), ["s7-expiring"]);
  assert.deepEqual(build([sOk], p), []);
});
