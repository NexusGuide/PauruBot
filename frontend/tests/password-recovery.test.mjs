import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync,mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {copy,text} from '../src/admin/password-recovery/copy.ts';
const source=readFileSync(new URL('../src/admin/password-recovery/PasswordRecovery.tsx',import.meta.url),'utf8');
test('all recovery copy is bilingual and states the existing-destination requirement',()=>{
 for(const [k,pair] of Object.entries(copy)){assert.equal(pair.length,2);assert(pair[0]&&pair[1],k);assert.notEqual(pair[0],pair[1],k)}
 assert.match(text('en','intro'),/already linked/);assert.match(text('fa','intro'),/ازقبل/);
});
test('login and account both expose the recovery form; secrets are not persisted or put in a URL',()=>{
 for(const name of ['Auth','System'])assert.match(readFileSync(new URL(`../src/admin/screens/${name}.tsx`,import.meta.url),'utf8'),/<PasswordRecovery/);
 assert(!/localStorage|sessionStorage|URLSearchParams|console\./.test(source));
 assert(source.includes('type="password" autoComplete="off"'));
});
const temp=mkdtempSync(new URL('./.reset-',import.meta.url));
try{
 const prelude=`import {text} from './copy.js';
 let values:any[]=[],index=0,cleanup:any,refreshes=0,writes=0;
 const useState=(v:any)=>{const i=index++;if(!(i in values))values[i]=v;return[values[i],(x:any)=>{writes++;values[i]=x}]};
 const useRef=(v:any)=>{const i=index++;if(!(i in values))values[i]={current:v};return values[i]};
 const useEffect=(fn:any)=>{if(!cleanup)cleanup=fn()};
 const useSession=()=>({locale:'en',refresh:async()=>{refreshes++}});
 const Button=()=>null,Field=()=>null,Input=()=>null;
 export const pending:any[]=[];
 const call=(path:any,options:any)=>new Promise((resolve,reject)=>pending.push({path,options,resolve,reject}));
 export function mount(){values=[];cleanup=null;refreshes=0;writes=0;return render()}
 export function render(){index=0;return PasswordRecovery({})}
 export const put=(i:any,v:any)=>{values[i]=v};
 export const state=()=>({values,refreshes,writes});export const unmount=()=>cleanup();
 `;
 writeFileSync(temp+'/Form.tsx',prelude+source.replace(/^import .*;\n/gm,''));
 writeFileSync(temp+'/copy.ts',readFileSync(new URL('../src/admin/password-recovery/copy.ts',import.meta.url)));
 execFileSync(process.execPath,[fileURLToPath(new URL('../node_modules/typescript/lib/tsc.js',import.meta.url)), '--ignoreConfig','--noCheck','--jsx','react-jsx','--module','esnext','--target','es2022','--outDir',temp,temp+'/Form.tsx',temp+'/copy.ts'],{stdio:'pipe'});
 const r=await import(pathToFileURL(temp+'/Form.js'));
 const settle=()=>new Promise(resolve=>setImmediate(resolve));
 const event={preventDefault(){}};
 const open=()=>{r.mount();r.put(0,true);r.put(1,'owner');return r.render()};
 const forms=tree=>tree.props.children.filter(v=>v?.type==='form');
 test('actual request handler deduplicates a double submit and never sends password/token',async()=>{
  const [form]=forms(open());const n=r.pending.length;
  form.props.onSubmit(event);form.props.onSubmit(event);assert.equal(r.pending.length,n+1);
  const p=r.pending.at(-1);assert.deepEqual(p.options.body,{username:'owner'});assert(p.path.endsWith('/request'));
  p.resolve({ok:true});await settle();assert.equal(r.state().values[6],'sent');assert.equal(r.state().refreshes,0);
 });
 test('actual confirmation validates matching passwords before network I/O',()=>{
  open();r.put(2,'x'.repeat(43));r.put(3,'new-password');r.put(4,'different');const n=r.pending.length;
  forms(r.render())[1].props.onSubmit(event);assert.equal(r.pending.length,n);assert.equal(r.state().values[7],'mismatch');
 });
 test('actual confirmation clears secrets and refreshes session only after explicit success',async()=>{
  open();r.put(2,'x'.repeat(43));r.put(3,'new-password');r.put(4,'new-password');const form=forms(r.render())[1];
  form.props.onSubmit(event);const p=r.pending.at(-1);assert(p.path.endsWith('/confirm'));
  assert.deepEqual(p.options.body,{token:'x'.repeat(43),new_password:'new-password'});
  p.resolve({ok:true});await settle();assert.deepEqual(r.state().values.slice(2,5),['','','']);assert.equal(r.state().refreshes,1);assert.equal(r.state().values[6],'done');
 });
 test('network failure or invalid server response does not claim completion',async()=>{
  for(const mode of ['network','bad-response']){
   const form=forms(open())[0];form.props.onSubmit(event);const p=r.pending.at(-1);
   if(mode==='network')p.reject(Error('network'));else p.resolve({ok:false});await settle();
   assert.equal(r.state().values[6],null);assert.equal(r.state().values[7],'failed');assert.equal(r.state().refreshes,0);
  }
 });
 test('unmounted component ignores late success without state writes or session refresh',async()=>{
  forms(open())[0].props.onSubmit(event);const p=r.pending.at(-1);r.unmount();const before=r.state().writes;
  p.resolve({ok:true});await settle();assert.equal(r.state().writes,before);assert.equal(r.state().refreshes,0);
 });
}finally{rmSync(temp,{recursive:true,force:true})}
