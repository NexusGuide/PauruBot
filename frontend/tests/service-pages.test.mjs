import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const {SERVICE_PAGE_SIZE,SERVICE_STATUSES,serviceFilter,serviceGroup,servicePageUrl,readServicePage,readServiceGroups,servicesReturnTo}=await import(new URL('../src/app/service-pages.ts',import.meta.url));
const rows=(n=20,top=100)=>Array.from({length:n},(_,i)=>({id:top-i}));
test('explicit bounded API query uses encoded literal search and status',()=>{
 const url=new URL(servicePageUrl('  تهران %_ &x=bad  ','active',123),'https://local');
 assert.equal(url.pathname,'/services');assert.equal(url.searchParams.get('q'),'تهران %_ &x=bad');assert.equal(url.searchParams.get('limit'),'20');assert.equal(url.searchParams.get('status'),'active');assert.equal(url.searchParams.get('before'),'123');assert.equal(url.searchParams.size,4);
});
test('blank filters are omitted; legacy api.services is separate',()=>assert.equal(servicePageUrl('   ','').includes('q='),false));
for(const value of [0,-1,NaN,Infinity,1.5,2**53])test(`reject invalid cursor ${value}`,()=>assert.throws(()=>servicePageUrl('','',value)));
for(const value of SERVICE_STATUSES)test(`supported status ${value}`,()=>assert.equal(serviceFilter(value),value));
for(const value of [null,'deleted','all','unknown','ACTIVE',''])test(`unknown/hidden status ${value} normalizes to all`,()=>assert.equal(serviceFilter(value),''));
test('bounded final and continuing pages preserve order and exact cursors',()=>{
 for(const n of [0,1,19,20])assert.equal(readServicePage({items:rows(n),next_before:null}).items.length,n);
 assert.equal(readServicePage({items:rows(),next_before:81}).next_before,81);
 assert.equal(SERVICE_PAGE_SIZE,20);assert.equal(readServicePage({items:rows(20,80),next_before:61},81).items[0].id,80);
});
for(const value of [null,[],{}, {items:[],next_before:1}, {items:rows(21),next_before:null}, {items:rows(),next_before:80}, {items:rows(19),next_before:82}, {items:[{id:0}],next_before:null}, {items:[{id:2**53}],next_before:null}, {items:[{id:2},{id:2}],next_before:null}, {items:[{id:1},{id:2}],next_before:null}, {items:[{}],next_before:null}, {items:[{id:'1'}],next_before:null}])test(`malformed page fails without silently appending ${JSON.stringify(value).slice(0,70)}`,()=>assert.throws(()=>readServicePage(value)));
test('overlapping page cannot reintroduce a previously loaded item',()=>assert.throws(()=>readServicePage({items:[{id:81}],next_before:null},81)));
for(const value of [null,undefined,{},'https://evil.test/services?q=a','//evil.test/services','/services/1?q=x','/services-extra?q=x','javascript:alert(1)'])test(`unsafe back target defaults to services ${String(value)}`,()=>assert.equal(servicesReturnTo(value),'/services'));
test('back target keeps only the bounded search and supported status',()=>assert.equal(servicesReturnTo('/services?q=+Tehran+&status=active&before=4&redirect=https://evil.test'),'/services?q=Tehran&status=active'));
test('oversized search is bounded in both API and return route',()=>{
 assert.equal(new URL(servicePageUrl('x'.repeat(121),''),'https://local').searchParams.get('q').length,120);
 assert.equal(new URL(servicesReturnTo('/services?q='+ 'x'.repeat(121)),'https://local').searchParams.get('q').length,120);
});
const source=readFileSync(new URL('../src/app/screens/Services.tsx',import.meta.url),'utf8');
test('list requests abort and guard obsolete generations; no local subset filtering',()=>{
 assert.match(source,/api\.servicePage\(q, status/);assert.match(source,/current\.signal\.aborted/);assert.match(source,/id === sequence\.current/);assert.match(source,/if \(!active\.current \|\| busy\.current \|\| nextBefore === null\)/);assert.doesNotMatch(source,/items\.filter\(/);
});
test('query identity resets the list, and detail stores a safe return target',()=>{
 assert.match(source,/key=\{JSON.stringify\(\[query, status, group\]\)\}/);assert.match(source,/state: \{ servicesReturnTo: returnTo \}/);assert.match(source,/backTo=\{backTo\}/);
});
for(const locale of ['fa','en'])test(`search controls are localized in ${locale}`,()=>{
 const copy=JSON.parse(readFileSync(new URL(`../src/app/locales/${locale}.json`,import.meta.url),'utf8'));
 for(const key of ['services_search','services_search_hint','services_status','services_clear','services_more','services_no_matches','services_no_matches_hint','services_group','services_group_all','services_group_other',...SERVICE_STATUSES.map(s=>'status_'+s)])assert(copy[key]?.length>0,key);
});
for(const [value,want] of [[null,null],['',null],['0',0],['7',7],['2147483647',2147483647],['2147483648',null],['-1',null],['1.5',null],['x',null],['12345678901',null]])test(`location group ${value} reads as ${want}`,()=>assert.equal(serviceGroup(value),want));
test('location group is sent with the page and kept in the back target',()=>{
 const url=new URL(servicePageUrl('','active',50,3),'https://local');assert.equal(url.searchParams.get('group'),'3');assert.equal(url.searchParams.get('before'),'50');
 assert.equal(new URL(servicePageUrl('',''),'https://local').searchParams.has('group'),false);
 assert.equal(new URL(servicePageUrl('','',undefined,0),'https://local').searchParams.get('group'),'0');
 assert.equal(servicesReturnTo('/services?group=0&q=a'),'/services?q=a&group=0');assert.equal(servicesReturnTo('/services?group=evil'),'/services');
});
test('groups show only when the services live in two or more places',()=>{
 const two={groups:[{id:4,title:'Germany',flag:'DE',count:2},{id:0,title:null,flag:null,count:1}]};
 assert.deepEqual(readServiceGroups(two),two.groups);
 assert.deepEqual(readServiceGroups({groups:[two.groups[0]]}),[]);
 for(const bad of [null,{},{groups:{}},{groups:[{id:-1,title:null,flag:null,count:1},two.groups[0]]},{groups:[{id:1,title:2,flag:null,count:1},two.groups[0]]},{groups:[{id:1,title:null,flag:null,count:0},two.groups[0]]}])assert.deepEqual(readServiceGroups(bad),[]);
});
test('the chosen group drives every page read and the chips',()=>{
 assert.match(source,/api\.servicePage\(q, status, undefined, current\.signal, group\)/);assert.match(source,/api\.servicePage\(q, status, before, current\.signal, group\)/);
 assert.match(source,/readServiceGroups\(value\)/);assert.match(source,/aria-pressed=\{group === g\.id\}/);
});
