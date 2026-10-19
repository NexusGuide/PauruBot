import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const { createActionGate, runServiceAction } = await import(new URL('../src/app/service-read.ts', import.meta.url));
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no}); return {promise,resolve,reject}; };

test('service actions cannot double-submit even before React renders busy state', async()=>{
 const gate=createActionGate(), pending=deferred(), seen=[]; let requests=0;
 const work=()=>{requests++;return pending.promise};
 const a=runServiceAction(gate,work,v=>seen.push(v),()=>assert.fail('unexpected error'),()=>seen.push('settled'));
 await runServiceAction(gate,work,()=>assert.fail('duplicate success'),()=>assert.fail('duplicate failure'),()=>assert.fail('duplicate settled'));
 assert.equal(requests,1);assert.equal(gate.busy,true);pending.resolve('links');await a;
 assert.deepEqual(seen,['links','settled']);assert.equal(gate.busy,false);
});
for(const failed of [false,true])test(`leaving a service ignores late ${failed?'failure':'success'} and cleanup`,async()=>{
 const gate=createActionGate(), pending=deferred(), seen=[];
 const a=runServiceAction(gate,()=>pending.promise,()=>seen.push('success'),()=>seen.push('error'),()=>seen.push('settled'));
 gate.close();if(failed)pending.reject(new Error('late'));else pending.resolve('late');await a;
 assert.deepEqual(seen,[]);assert.equal(gate.busy,false);assert.equal(gate.begin(),false);
});
test('failure is caught and the same screen can retry successfully',async()=>{
 const gate=createActionGate(), seen=[];
 await runServiceAction(gate,async()=>{throw new Error('offline')},()=>assert.fail(),e=>seen.push(e.message),()=>seen.push('settled'));
 await runServiceAction(gate,async()=>['config'],v=>seen.push(v[0]),()=>assert.fail(),()=>seen.push('settled'));
 assert.deepEqual(seen,['offline','settled','config','settled']);
});
test('synchronous transport failure also unlocks the screen',async()=>{
 const gate=createActionGate();let errors=0;
 await runServiceAction(gate,()=>{throw new Error('sync')},()=>assert.fail(),()=>errors++,()=>{});
 assert.equal(errors,1);assert.equal(gate.begin(),true);
});
test('closed screen cannot send another network request',async()=>{
 const gate=createActionGate();gate.close();
 await runServiceAction(gate,()=>assert.fail('no remote request'),()=>assert.fail(),()=>assert.fail(),()=>assert.fail());
});
test('a new service has an independent gate; old completion cannot unlock its busy action',async()=>{
 const old=createActionGate(), next=createActionGate(), a=deferred(), b=deferred();
 const first=runServiceAction(old,()=>a.promise,()=>assert.fail(),()=>assert.fail(),()=>assert.fail());
 old.close();const second=runServiceAction(next,()=>b.promise,()=>{},()=>assert.fail(),()=>{});
 a.resolve('old');await first;assert.equal(next.busy,true);b.resolve('new');await second;assert.equal(next.busy,false);
});
const source=readFileSync(new URL('../src/app/screens/Services.tsx',import.meta.url),'utf8');
test('detail and extra screen instances are keyed by the route resource',()=>{
 assert.match(source,/<ServiceDetailBody key=\{id\}/);assert.match(source,/<ExtraBody key=\{`\$\{id\}:\$\{unit\}`\}/);
 assert.doesNotMatch(source,/\.catch\(\(\) => navigate\("\/services"\)\)/);
});
test('QR uses a caught resource read, keyed by its actual link and refreshed after revoke',()=>{
 assert.match(source,/const \{ data, error, loading, retry \} = useResource\(load\)/);
 assert.match(source,/<ServiceQr key=\{service.subscription_url\}/);
 assert.match(source,/key=\{`wg:\$\{service.id\}:\$\{qrVersion\}`\}/);
 assert.match(source,/setQrVersion\(value => value \+ 1\)/);
 assert.doesNotMatch(source,/\.then\(setQr\)|\.then\(setWgQr\)/);
});
test('links failure has a retry and both remote-action controls disable while busy',()=>{
 assert.match(source,/ReadFailure error=\{linksError\} retry=/);
 assert.equal([...source.matchAll(/disabled=\{busy !== null\}/g)].length,2);
 assert.match(source,/const showLinks = \(\) => runServiceAction/);
});
for(const locale of ['fa','en'])test(`QR loading and WireGuard alt are translated in ${locale}`,()=>{
 const copy=JSON.parse(readFileSync(new URL(`../src/app/locales/${locale}.json`,import.meta.url),'utf8'));
 for(const key of ['service_qr_loading','wireguard_qr_alt','service_qr_empty'])assert(copy[key]?.length>0);
});
