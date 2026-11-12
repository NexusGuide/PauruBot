import assert from "node:assert/strict";
import test from "node:test";
import { screenHarness, nodes, deferred, tick } from "./helpers/wallet-screen.mjs";
const url = path => new URL(`../src/app/${path}`, import.meta.url);
const sessions = screenHarness().compile(url("transfer-session.ts"), {});
const q = {token:"opaque-server-consent",amount:50,currency:"IRT",expires_at:2_000_000_000,recipient:{telegram_id:200,name:"Receiver",username:"recipient"}};
const storage = () => { const values = new Map(); return {getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)}; };
class ApiError extends Error {constructor(status,code){super(code);this.status=status;this.code=code;}}
function setup({policy=async()=>({enabled:true,minimum:10,maximum:100,daily_maximum:100,currency:"IRT"}),quote=async()=>q,confirm=async()=>({journal_id:42}),store=storage(),owner=1}={}) {
 Object.defineProperty(globalThis,"sessionStorage",{value:store,configurable:true});
 const h=screenHarness();let held=false,done=0;
 const props={owner,disabled:false,acquire:()=>{if(held)return false;held=true;return true},release:()=>{held=false},onDone:()=>{done++}};
 const calls={quote:[],confirm:[]};
 const {WalletTransfer}=h.compile(url("screens/WalletTransfer.tsx"),{
  "@shared/http":{ApiError},"@shared/format":{money:n=>String(n)},
  "../state":{useApp:()=>({locale:"en"})},"../ui":{Button:"Button"},"../transfer-session":sessions,
  "../api":{api:{transferPolicy:policy,transferQuote:(...args)=>{calls.quote.push(args);return quote(...args)},transferConfirm:(token)=>{calls.confirm.push(token);return confirm(token)}}},
 });
 return {...h,props,calls,done:()=>done,held:()=>held,render:()=>h.render(WalletTransfer,props)};
}
function button(tree,label){return nodes(tree,n=>n.type==="Button" && n.props.children===label)[0];}
async function ready(h){h.render();await tick();return h.render();}
async function preview(h){let tree=await ready(h);const inputs=nodes(tree,n=>n.type==="input");inputs[0].props.onChange({target:{value:"@recipient"}});inputs[1].props.onChange({target:{value:"50"}});tree=h.render();await button(tree,"Preview recipient and amount").props.onClick();return h.render();}
for (const value of ["50","۵۰","٥٠"]) test(`integer amount accepts ${value}`,()=>assert.equal(sessions.transferAmount(value),50));
for (const value of ["-1","1e3","5.5","","Infinity","9007199254740992"]) test(`reject malformed amount ${value}`,()=>assert.equal(sessions.transferAmount(value),null));
test("session token is scoped by authenticated owner and survives reload",()=>{Object.defineProperty(globalThis,"sessionStorage",{value:storage(),configurable:true,writable:true});sessions.saveTransfer(1,{quote:q,attempted:true});assert.deepEqual(sessions.readTransfer(1),{quote:q,attempted:true});assert.equal(sessions.readTransfer(2),null);sessions.clearTransfer(1);assert.equal(sessions.readTransfer(1),null);});
test("disabled policy hides new transfer; a failed read exposes read-only retry",async()=>{
 let calls=0;const h=setup({policy:async()=>{if(++calls===1)throw Error("offline");return{enabled:false}}});
 let tree=await ready(h);assert(button(tree,"Retry policy read"));await button(tree,"Retry policy read").props.onClick();assert.equal(h.render(),null);assert.equal(h.calls.confirm.length,0);h.unmount();
});
test("preview has no financial effect and displays exact recipient, currency, amount",async()=>{
 const h=setup();const tree=await preview(h);assert.deepEqual(h.calls.quote,[["@recipient",50]]);assert.equal(h.calls.confirm.length,0);
 assert(button(tree,"Confirm transfer"));assert(JSON.stringify(tree).includes("Telegram ID: "));assert(JSON.stringify(tree).includes("200"));assert(JSON.stringify(tree).includes("IRT"));h.unmount();
});
test("same tick duplicate confirmations are blocked and one acknowledgement refreshes reads",async()=>{
 const p=deferred(),h=setup({confirm:()=>p.promise});let tree=await preview(h);const action=button(tree,"Confirm transfer").props.onClick;
 const first=action();await action();assert.equal(h.calls.confirm.length,1);assert(h.held());p.resolve({journal_id:42});await first;tree=h.render();
 assert.equal(h.done(),1);assert.equal(h.held(),false);assert.equal(sessions.readTransfer(1),null);assert(JSON.stringify(tree).includes("42"));h.unmount();
});
test("lost response and reload preserve exact quote, block cancel and fresh transfer",async()=>{
 const store=storage();let h=setup({store,confirm:async()=>{throw Error("offline")}});let tree=await preview(h);
 await button(tree,"Confirm transfer").props.onClick();tree=h.render();assert(button(tree,"Retry same confirmation"));assert.equal(button(tree,"Cancel"),undefined);assert.equal(button(tree,"Preview recipient and amount"),undefined);
 h.unmount();h=setup({store});tree=await ready(h);await button(tree,"Retry same confirmation").props.onClick();assert.deepEqual(h.calls.confirm,[q.token]);assert.equal(h.calls.quote.length,0);h.unmount();
});
test("only server-confirmed unposted expiry releases an attempted token",async()=>{
 const h=setup({confirm:async()=>{throw new ApiError(409,"transfer_quote_expired")}});const tree=await preview(h);await button(tree,"Confirm transfer").props.onClick();assert.equal(sessions.readTransfer(1),null);assert(button(h.render(),"Preview recipient and amount"));assert.equal(h.done(),0);h.unmount();
});
test("unavailable storage fails closed before moving money",async()=>{
 const store={getItem(){throw Error()},setItem(){throw Error()},removeItem(){throw Error()}};const h=setup({store});const tree=await ready(h);assert.equal(button(tree,"Preview recipient and amount"),undefined);assert.equal(h.calls.confirm.length,0);h.unmount();
});
test("storage failure while marking attempted never sends confirm",async()=>{
 const store=storage(),h=setup({store});const tree=await preview(h);store.setItem=()=>{throw Error("quota")};await button(tree,"Confirm transfer").props.onClick();assert.equal(h.calls.confirm.length,0);assert.equal(h.done(),0);h.unmount();
});
test("unmounted owner ignores a late financial acknowledgement",async()=>{
 const p=deferred(),h=setup({confirm:()=>p.promise});const tree=await preview(h);const pending=button(tree,"Confirm transfer").props.onClick();h.unmount();p.resolve({journal_id:42});await pending;assert.equal(h.done(),0);assert.equal(h.held(),false);
});
test("disabled policy can still show and safely retry a previously attempted token",async()=>{
 const store=storage();Object.defineProperty(globalThis,"sessionStorage",{value:store,configurable:true,writable:true});sessions.saveTransfer(1,{quote:q,attempted:true});const h=setup({store,policy:async()=>({enabled:false})});const tree=await ready(h);assert(button(tree,"Retry same confirmation"));assert.equal(button(tree,"Preview recipient and amount"),undefined);h.unmount();
});
test("cancel before confirmation has no financial call and clears saved quote",async()=>{const h=setup();const tree=await preview(h);button(tree,"Cancel").props.onClick();assert.equal(sessions.readTransfer(1),null);assert.equal(h.calls.confirm.length,0);h.unmount();});

for (const phase of ["quote", "confirm"]) for (const code of ["transfer_off", "new_server_code"]) {
 test(`${phase} errors always render a concrete string: ${code}`,async()=>{
  const failure=async()=>{throw new ApiError(400,code)};
  const h=setup(phase==="quote"?{quote:failure}:{confirm:failure});
  if(phase==="quote") await preview(h);
  else {const tree=await preview(h);await button(tree,"Confirm transfer").props.onClick();}
  const messages=nodes(h.render(),n=>n.props.role==="status");
  assert.equal(messages.length,1);assert.equal(typeof messages[0].props.children,"string");
  assert(messages[0].props.children.length>0);
  assert.equal(messages[0].props.children,code==="transfer_off"?"Transfers are disabled for this shop.":"Result unknown or request failed. Retry this same confirmation; do not start another transfer.");
  h.unmount();
 });
}
