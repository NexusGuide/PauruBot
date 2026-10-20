import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
const {actionGate,TopupIntents}=await import('../src/app/topup-intents.ts');
function store() { const m = new Map(); return {getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k)}; }
let n=0;const uuid=()=>`test-key-${++n}`;
test('same task duplicate click is rejected before any render',()=>{const g=actionGate();assert(g.enter());assert(!g.enter());g.leave();assert(g.enter());});
test('network failure or reload reuses the existing opaque key',()=>{const s=store();const a=new TopupIntents(s,uuid);const key=a.get(1,100000,'card');assert.equal(a.get(1,100000,'card'),key);assert.equal(new TopupIntents(s,uuid).get(1,100000,'card'),key);});
test('successful checkout allows an intentional new topup',()=>{const a=new TopupIntents(store(),uuid);const key=a.get(1,100000,'card');a.clear(1,100000,'card',key);assert.notEqual(a.get(1,100000,'card'),key);});
test('different customers amounts and methods do not share keys',()=>{const a=new TopupIntents(store(),uuid);assert.equal(new Set([[1,100,'card'],[2,100,'card'],[1,200,'card'],[1,100,'stars']].map(x=>a.get(...x))).size,4);});
test('storage failure keeps an in-memory key',()=>{const s={getItem(){throw Error()},setItem(){throw Error()},removeItem(){throw Error()}};const a=new TopupIntents(s,uuid);const k=a.get(1,100,'card');assert.equal(a.get(1,100,'card'),k);a.clear(1,100,'card',k);assert.notEqual(a.get(1,100,'card'),k);});
test('late clear cannot erase a newer intent',()=>{const a=new TopupIntents(store(),uuid);const old=a.get(1,100,'card');a.clear(1,100,'card',old);const fresh=a.get(1,100,'card');a.clear(1,100,'card',old);assert.equal(a.get(1,100,'card'),fresh);});
test('invalid stored key is replaced',()=>{const s=store();s.setItem('pauru.topup.v1:1:100:card','bad');assert.notEqual(new TopupIntents(s,uuid).get(1,100,'card'),'bad');});
test('Wallet wires both handlers through a synchronous gate and disables same method',()=>{const s=readFileSync(new URL('../src/app/screens/Wallet.tsx',import.meta.url),'utf8');assert.equal((s.match(/!gate.current.enter\(\)/g)||[]).length,2);assert.equal((s.match(/gate.current.leave\(\)/g)||[]).length,2);assert(s.includes('disabled={!valid || busy !== null}'));assert(s.includes('api.topup(chosen, method, intent)'));});

// Execute the actual Wallet handlers with controlled hook/API seams, not just a copied gate.
const {mkdtempSync,writeFileSync,rmSync}=await import('node:fs');
const {fileURLToPath,pathToFileURL}=await import('node:url');
const {execFileSync}=await import('node:child_process');
const temp=mkdtempSync(new URL('./.topup-',import.meta.url));
try {
 let source=readFileSync(new URL('../src/app/screens/Wallet.tsx',import.meta.url),'utf8');
 source=source.replace(/^import .*;\n/gm,'');
 const helper=fileURLToPath(new URL('../src/app/topup-intents.ts',import.meta.url));
 writeFileSync(temp+'/helper.ts',readFileSync(helper,'utf8'));
 source=`import {actionGate,TopupIntents} from './helper.js';
 const intents=new TopupIntents(undefined,()=> 'controlled-intent-123');
 const topupIntents=()=>intents;
 const stateValues=[0,100000,'',null,'GIFT'];let stateIndex=0;
 const useState=()=>[stateValues[stateIndex++],()=>{}];
 const useRef=(current:any)=>({current}); const useEffect=()=>{};
 const useNavigate=()=>()=>{};
 class ApiError extends Error { status=0; code=''; }
 const me={id:1,balance:0,topup_bounds:[10000,1000000],payment_methods:['card','stars']};
 const useApp=()=>({me,t:(s:string)=>s,locale:'en',toast:()=>{},reload:async()=>{}});
 export const calls:any[]=[];let finish:()=>void=()=>{};
 export const resolve=()=>finish();
 const api={history:async()=>({items:[]}),redeemGift:async()=>{calls.push(['gift']);return{amount:10}},
 topup:(...args:any[])=>{calls.push(args);return new Promise(r=>{finish=()=>r({payment:{}})})}};
 const haptic=()=>{},openExternal=()=>{},openInvoice=async()=> 'paid';
 const errorText=()=>'',dateTime=()=>'',money=()=>'',rememberCard=()=>{},rememberPayment=()=>{};
 const Icon=()=>null,Button=()=>null,CountUp=()=>null,Screen=()=>null,PendingPayments=()=>null;
 // History read behavior is covered by wallet-history.test.mjs; this seam exercises payment handlers.
 export const WalletHistory=()=>null;
 export const Item=()=>null;
 `+source;
 writeFileSync(temp+'/Wallet.tsx',source);
 execFileSync(process.execPath,[fileURLToPath(new URL('../node_modules/typescript/lib/tsc.js',import.meta.url)),
 '--ignoreConfig','--noCheck','--jsx','react-jsx','--module','esnext','--target','es2022','--outDir',temp,temp+'/Wallet.tsx',temp+'/helper.ts'],{stdio:'pipe'});
 const runtime=await import(pathToFileURL(temp+'/Wallet.js'));
 function walk(x,found=[]){if(Array.isArray(x))for(const c of x)walk(c,found);else if(x&&typeof x==='object'&&x.props){found.push(x);walk(x.props.children,found)}return found;}
 test('actual Wallet handlers block repeated same-method, other-method and gift actions until settled',async()=>{
   const nodes=walk(runtime.Wallet());const methods=nodes.filter(n=>n.type===runtime.Item);
   const history=nodes.filter(n=>n.type===runtime.WalletHistory);
   assert.equal(history.length,1);assert.equal(history[0].key,'1:0');
   const redeem=nodes.find(n=>n.props.children==='redeem');assert.equal(methods.length,2);assert(redeem);
   const first=methods[0].props.onClick();
   await methods[0].props.onClick();await methods[1].props.onClick();redeem.props.onClick();
   assert.equal(runtime.calls.length,1);assert.equal(runtime.calls[0][1],'card');assert.equal(runtime.calls[0][2],'controlled-intent-123');
   runtime.resolve();await first;
   const next=methods[1].props.onClick();assert.equal(runtime.calls.length,2);runtime.resolve();await next;
 });
} finally {rmSync(temp,{recursive:true,force:true})}
