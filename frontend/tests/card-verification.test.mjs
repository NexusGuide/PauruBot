import test from "node:test";
import assert from "node:assert/strict";
import { parseCard, parseVerification, normalizeNumber, validFile, MAX_PHOTO, submissionGate } from "../src/app/cards/model.ts";
import { cardCopy } from "../src/app/cards/copy.ts";
import { tabForPath, parentForPath } from "../src/app/navigation.ts";
const card = () => ({ id: 1, masked: "4242-42**-****-4242", status: "pending" });
const state = () => ({ enabled: true, min_amount: 0, trusted: false, blocked: false, approved: false, can_submit: false, items: [card()] });
test("strict safe projection discards backend internals", () => {
  assert.deepEqual(parseCard({ ...card(), digest: "secret", photo_file_id: "secret" }), card());
  assert.deepEqual(parseVerification({ ...state(), customer_id: 90 }), state());
});
for (const bad of [null, {}, { ...card(), id: "1" }, { ...card(), id: 0 }, { ...card(), status: "unknown" },
  { ...card(), masked: "4242424242424242" }]) {
  test(`reject malformed card ${JSON.stringify(bad)}`, () => assert.throws(() => parseCard(bad)));
}
for (const changes of [{ enabled: "true" }, { min_amount: -1 }, { min_amount: "0" }, { items: [card(), card()] },
  { can_submit: true }, { can_submit: true, items: [], trusted: true }, { can_submit: true, items: [], approved: true },
  { can_submit: true, items: [], blocked: true }, { can_submit: true, items: [], enabled: false }]) {
  test(`fail closed ${JSON.stringify(changes)}`, () => assert.throws(() => parseVerification({ ...state(), ...changes })));
}
test("number normalization supports Persian/Arabic without dropping letters", () => {
  for (const text of ["4242-4242 4242-4242", "۴۲۴۲۴۲۴۲۴۲۴۲۴۲۴۲", "٤٢٤٢٤٢٤٢٤٢٤٢٤٢٤٢"]) {
    assert.equal(normalizeNumber(text), "4242424242424242");
  }
  for (const bad of ["4242424242424241", "a4242424242424242", "4242\n424242424242", "123"]) assert.equal(normalizeNumber(bad), null);
});
test("bounded JPEG/PNG selection only", () => {
  assert(validFile({ type: "image/png", size: MAX_PHOTO }));
  for (const file of [{ type: "image/svg+xml", size: 1 }, { type: "image/jpeg", size: MAX_PHOTO + 1 }, { type: "image/png", size: 0 }]) assert(!validFile(file));
});
test("rapid clicks and uncertain retry retain the exact payload", () => {
  const gate = submissionGate(), original = { intent_key: "one", number: "secret", image: "photo" };
  assert.equal(gate.enter(original), original);
  assert.equal(gate.enter({ ...original, intent_key: "two" }), null);
  gate.leave();
  assert.equal(gate.enter({ intent_key: "new", number: "changed", image: "changed" }), original);
  gate.leave(); gate.clear();
  const next = { ...original, intent_key: "explicit-new-after-definite-result" };
  assert.equal(gate.enter(next), next);
});
test("navigation stays under wallet and translations share keys", () => {
  assert.equal(tabForPath("/card-verification"), "wallet");
  assert.equal(parentForPath("/card-verification"), "/wallet");
  assert.deepEqual(Object.keys(cardCopy("fa")), Object.keys(cardCopy("en")));
});

// Execute the actual form handler and lifecycle with controlled hooks/I/O (no Telegram/provider).
const { mkdtempSync, readFileSync, writeFileSync, rmSync } = await import("node:fs");
const { fileURLToPath, pathToFileURL } = await import("node:url");
const { execFileSync } = await import("node:child_process");
const temp = mkdtempSync(new URL("./.card-", import.meta.url));
try {
  let source = readFileSync(new URL("../src/app/screens/CardVerification.tsx", import.meta.url), "utf8").replace(/^import .*;\n/gm, "");
  writeFileSync(temp + "/model.ts", readFileSync(new URL("../src/app/cards/model.ts", import.meta.url), "utf8"));
  writeFileSync(temp + "/copy.ts", readFileSync(new URL("../src/app/cards/copy.ts", import.meta.url), "utf8"));
  source = `import { normalizeNumber, parseCard, parseVerification, submissionGate, validFile, type Verification } from './model.js';
import { cardCopy } from './copy.js';
let hooks:any[]=[], cursor=0, effects:(()=>void)[]=[];
const same=(a:any[],b:any[])=>a&&a.length===b.length&&a.every((x,i)=>x===b[i]);
const useState=(initial:any)=>{const i=cursor++;if(!(i in hooks))hooks[i]=initial;return [hooks[i],(value:any)=>{hooks[i]=typeof value==='function'?value(hooks[i]):value}];};
const useRef=(value:any)=>{const i=cursor++;return hooks[i]??=( {current:value} );};
const useCallback=(fn:any,deps:any[])=>{const i=cursor++;if(!hooks[i]||!same(hooks[i].deps,deps))hooks[i]={fn,deps};return hooks[i].fn;};
const useEffect=(fn:any,deps:any[])=>{const i=cursor++;if(!hooks[i]||!same(hooks[i].deps,deps)){effects.push(()=>{hooks[i]?.cleanup?.();hooks[i]={deps,cleanup:fn()}})}};
export let locale='en';export const setLocale=(v:string)=>{locale=v};
const useApp=()=>({me:{id:1},locale});const useNavigate=()=>()=>{};
const Button=()=>null,Panel=()=>null,Screen=()=>null,money=()=>'';
class ApiError extends Error {status=0;}
let pending:any={enabled:true,min_amount:0,trusted:false,blocked:false,approved:false,can_submit:true,items:[]};
export const calls:any[]=[];let finish:any,fail:any,reader:any;
const api={cardVerification:async()=>pending,submitCardVerification:(body:any)=>{calls.push({...body});return new Promise((ok,no)=>{finish=ok;fail=no})}};
class FileReader {result:any;onload:any;onerror:any;readAsDataURL(){reader=this;}}
export const readPhoto=()=>{reader.result='data:image/png;base64,photo-base64';reader.onload()};
export const reject=()=>fail(new Error('network'));
export const resolve=()=>{pending={...pending,can_submit:false,items:[{id:1,masked:'4242-42**-****-4242',status:'pending'}]};finish({card:pending.items[0]})};
export function render(){cursor=0;const result=CardForm();for(const fn of effects.splice(0))fn();return result;}
export function cleanup(){for(const h of hooks)h?.cleanup?.();}
export function reset(){cleanup();hooks=[];cursor=0;effects=[];calls.length=0;pending={...pending,can_submit:true,items:[]};locale='en';}
` + source;
  writeFileSync(temp + "/Form.tsx", source);
  execFileSync(process.execPath, [fileURLToPath(new URL("../node_modules/typescript/lib/tsc.js", import.meta.url)),
    "--ignoreConfig", "--noCheck", "--jsx", "react-jsx", "--module", "esnext", "--target", "es2022", "--outDir", temp,
    temp + "/Form.tsx", temp + "/model.ts", temp + "/copy.ts"], { stdio: "pipe" });
  const runtime = await import(pathToFileURL(temp + "/Form.js"));
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  function walk(node, result = []) {
    if (Array.isArray(node)) for (const child of node) walk(child, result);
    else if (node && typeof node === "object" && node.props) { result.push(node); walk(node.props.children, result); }
    return result;
  }
  async function fill() {
    runtime.reset(); runtime.render(); await tick();
    const nodes = walk(runtime.render());
    nodes.find((n) => n.type === "input" && n.props.type === "text").props.onChange({ target: { value: "4242424242424242" } });
    nodes.find((n) => n.type === "input" && n.props.type === "file").props.onChange({ target: { files: [{ type: "image/png", size: 10 }] } });
    return walk(runtime.render()).find((n) => n.type === "form");
  }
  test("actual form blocks duplicate clicks and keeps exact retry across locale changes", async () => {
    const form = await fill();
    const click = () => form.props.onSubmit({ preventDefault() {} });
    click(); click();
    assert.equal(runtime.calls.length, 0);
    runtime.readPhoto(); await tick();
    assert.equal(runtime.calls.length, 1);
    const original = runtime.calls[0];
    runtime.reject(); await tick();
    runtime.setLocale("fa"); runtime.render(); await tick();
    const retry = walk(runtime.render()).find((n) => n.type === "form");
    retry.props.onSubmit({ preventDefault() {} }); await tick();
    assert.deepEqual(runtime.calls[1], original);
    runtime.resolve(); await tick();
    assert(!walk(runtime.render()).some((n) => n.type === "form"));
    runtime.cleanup();
  });
  test("actual form unmount during file read cannot submit with the old principal", async () => {
    const form = await fill();
    form.props.onSubmit({ preventDefault() {} }); runtime.cleanup();
    runtime.readPhoto(); await tick();
    assert.equal(runtime.calls.length, 0);
  });
} finally { rmSync(temp, { recursive: true, force: true }); }
