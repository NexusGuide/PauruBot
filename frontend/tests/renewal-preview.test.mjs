import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync, mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {decodePreview, fresh, previewTime} from '../src/app/renewal/model.ts';
const expected={id:10,service_id:20,plan_id:30,amount:90000};
function sample(){const now=Date.now();return {order_id:10,service_id:20,plan_id:30,amount:90000,
 computed_at:new Date(now).toISOString(),valid_until:new Date(now+60000).toISOString(),basis:'stored_service_snapshot',
 timing:'immediate',queued_until:null,policy:{time:'append',volume:'reset',reset_usage:true},resets_usage:true,
 before:{expire_at:null,data_limit:100,used_bytes:40,remaining_bytes:60},
 after:{expire_at:null,data_limit:200,used_bytes:0,remaining_bytes:200}};}
test('valid immediate, queued and unlimited projections retain distinct meanings',()=>{
 assert.equal(decodePreview(sample(),expected).after.remaining_bytes,200);
 const q={...sample(),timing:'queued',queued_until:new Date().toISOString(),after:null};
 assert.equal(decodePreview(q,expected).after,null);
 const u=sample();u.after={expire_at:null,data_limit:0,used_bytes:0,remaining_bytes:null};
 assert.equal(decodePreview(u,expected).after.remaining_bytes,null);
});
for(const [name,change] of Object.entries({
 'wrong order':v=>v.order_id++, 'wrong service':v=>v.service_id++, 'wrong plan':v=>v.plan_id++,
 'wrong price':v=>v.amount++, 'wrong basis':v=>v.basis='live_panel', 'invalid date':v=>v.before.expire_at='not a date',
 'negative bytes':v=>v.before.used_bytes=-1, 'inconsistent remainder':v=>v.before.remaining_bytes=0,
 'invented queued target':v=>{v.timing='queued';v.queued_until=new Date().toISOString()},
 'unlimited as zero remaining':v=>{v.before.data_limit=0;v.before.remaining_bytes=0},
 'unsafe integer':v=>v.after.data_limit=Number.MAX_SAFE_INTEGER+10,
 'wrong reset counter':v=>{v.after.used_bytes=10;v.after.remaining_bytes=190},
 'bad rule':v=>v.policy.time='increase', 'missing boolean':v=>delete v.resets_usage,
 'too long validity':v=>v.valid_until=new Date(Date.parse(v.computed_at)+61000).toISOString(),
 'expired interval':v=>v.valid_until=v.computed_at,
})) test(`decoder rejects ${name}`,()=>{const v=sample();change(v);assert.throws(()=>decodePreview(v,expected));});
test('timestamps distinguish years and include the device timezone in both locales',()=>{
 for(const locale of ['en','fa']) assert.notEqual(previewTime('2026-01-10T13:45:00Z',locale),previewTime('2027-01-10T13:45:00Z',locale));
 assert.match(previewTime('2026-01-10T13:45:00Z','en'),/2026/);
 const source=readFileSync(new URL('../src/app/renewal/Preview.tsx',import.meta.url),'utf8');
 assert(source.includes('size(n, locale, t("units").split(","))')); // small byte counts must not round to 0 GB
});
test('freshness fails for expired or implausibly future previews',()=>{
 const v=sample(),now=Date.now();assert(fresh(v,now));assert(!fresh(v,now+61000));assert(!fresh(v,now-60000));
});
test('all preview labels and placeholders match in English and Persian',()=>{
 const en=JSON.parse(readFileSync(new URL('../src/app/locales/en.json',import.meta.url)));
 const fa=JSON.parse(readFileSync(new URL('../src/app/locales/fa.json',import.meta.url)));
 for(const key of Object.keys(en).filter(k=>k.startsWith('renew_preview_')||k.startsWith('err_renewal_preview_'))){
   assert(fa[key],key);assert.deepEqual(en[key].match(/\{\w+\}/g)||[],fa[key].match(/\{\w+\}/g)||[],key);
 }
});
test('checkout gates wallet and every external method on this order preview, without changing other kinds',()=>{
 const s=readFileSync(new URL('../src/app/screens/Checkout.tsx',import.meta.url),'utf8');
 const expression=s.match(/const renewalReady = (.*);/)[1];
 const ready=new Function('order','previewedOrderId',`return ${expression};`);
 for (const kind of ['purchase','renew']) for (const national of [false,true]) {
   const order={id:10,kind,params:{national}};
   for (const previewId of [null,9,10]) assert.equal(ready(order,previewId),
     kind !== 'renew' || national || previewId === order.id,
     `${kind}, national=${national}, preview=${previewId}`);
 }
 assert(s.includes('order.kind === "renew" && !order.params.national && <RenewalPreview'));
 assert(s.includes('if (!renewalReady) return;'));
 assert(s.includes('disabled={!renewalReady || (due > 0 && methods.length === 0)}'));
 assert(s.includes('disabled={busy !== null || !renewalReady} onClick={() => pay(option)}'));
 assert(s.includes('<RenewalPreview order={order} onReady={setPreviewedOrderId} />'));
});

// Execute the actual React component's effects with controlled hooks/API/timer seams.
const temp=mkdtempSync(new URL('./.renewal-',import.meta.url));
try{
 let source=readFileSync(new URL('../src/app/renewal/Preview.tsx',import.meta.url),'utf8').replace(/^import .*;\n/gm,'');
 const prelude=`import {decodePreview,fresh,previewTime} from './model.js';
 let states:any[]=[],index=0,effect:any;
 const useState=(initial:any)=>{const i=index++;if(!(i in states))states[i]=initial;return[states[i],(v:any)=>{states[i]=typeof v==='function'?v(states[i]):v}]};
 const useEffect=(fn:any)=>{effect=fn};
 export const pending:any[]=[];export const timers:any[]=[];
 const api={renewalPreview:(id:any,signal:any)=>new Promise((resolve,reject)=>pending.push({id,signal,resolve,reject}))};
 const setTimeout=(fn:any)=>{timers.push(fn);return timers.length};const clearTimeout=()=>{};
 const useApp=()=>({t:(key:any)=>key,locale:'en'});const size=(v:any)=>String(v);
 const Button=()=>null,Panel=()=>null;
 export function mount(order:any,onReady:any){states=[];index=0;RenewalPreview({order,onReady});return effect()}
 `;
 writeFileSync(temp+'/Preview.tsx',prelude+source);
 writeFileSync(temp+'/model.ts',readFileSync(new URL('../src/app/renewal/model.ts',import.meta.url)));
 execFileSync(process.execPath,[fileURLToPath(new URL('../node_modules/typescript/lib/tsc.js',import.meta.url)),
 '--ignoreConfig','--noCheck','--jsx','react-jsx','--module','esnext','--target','es2022','--outDir',temp,temp+'/Preview.tsx',temp+'/model.ts'],{stdio:'pipe'});
 const r=await import(pathToFileURL(temp+'/Preview.js'));
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 test('actual component disables on load, enables only validated matching result, and expires readiness',async()=>{
  const calls=[];const cleanup=r.mount(expected,v=>calls.push(v));assert.deepEqual(calls,[null]);
  r.pending.at(-1).resolve(sample());await settle();assert.deepEqual(calls,[null,10]);
  r.timers.at(-1)();assert.equal(calls.at(-1),null);cleanup();
 });
 test('unmount aborts request and late response cannot enable payment',async()=>{
  const calls=[];const cleanup=r.mount(expected,v=>calls.push(v));const p=r.pending.at(-1);cleanup();
  assert(p.signal.aborted);p.resolve(sample());await settle();assert(!calls.includes(10));
 });
 test('network failure and wrong-order response never enable payment',async()=>{
  for(const bad of ['network','wrong-order']){const calls=[];const cleanup=r.mount(expected,v=>calls.push(v));
   const p=r.pending.at(-1);if(bad==='network')p.reject(Error('network'));else p.resolve({...sample(),order_id:999});
   await settle();assert(!calls.includes(10));cleanup();}
 });
}finally{rmSync(temp,{recursive:true,force:true})}
