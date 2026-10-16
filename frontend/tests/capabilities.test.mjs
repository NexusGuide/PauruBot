import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
const {validateReport,needsCleanup,cleanupUnconfirmed,CHECK_KEYS}=await import(new URL('../src/admin/capabilities/report.ts',import.meta.url));
const read=()=>({status:'ok',write_tested:false,checks:[{key:'connect',status:'ok',detail:'Version 1'},{key:'client_ips',status:'skip',detail:''}]});
test('read-only report preserves skipped and raw plain details',()=>{const d=read();assert.deepEqual(validateReport(d),d);assert(!needsCleanup(d))});
for(const [status,expected] of [['ok','ok'],['warn','warn'],['fail','fail'],['skip','ok']])test(`overall ${expected} from ${status}`,()=>{const d=read();d.checks[0].status=status;d.status=expected;assert.deepEqual(validateReport(d),d)});
test('write-create failure valid and cleanup not assumed',()=>{const d={status:'fail',write_tested:true,checks:[{key:'connect',status:'ok',detail:''},{key:'create',status:'fail',detail:'Denied'}]};assert.deepEqual(validateReport(d),d);assert(!needsCleanup(d))});
test('delete failure requires manual cleanup even after successful creation',()=>{const d={status:'fail',write_tested:true,checks:[{key:'create',status:'ok',detail:'pbprobe_ab12cd'},{key:'delete',status:'fail',detail:'remove pbprobe_ab12cd by hand'}]};assert(needsCleanup(validateReport(d)))});
test('unknown checks and HTML-looking errors remain bounded plain strings',()=>{const d=read();d.checks.push({key:'future_feature',status:'warn',detail:'<img src=x onerror=alert(1)>'});d.status='warn';assert.equal(validateReport(d).checks[2].detail,d.checks[2].detail)});
test('only explicit fields projected; never spread unsolicited API extras',()=>{const d=read();d.password='DO_NOT_PROJECT';d.checks[0].api_token='DO_NOT_PROJECT';const result=validateReport(d);assert(!('password' in result));assert(!('api_token' in result.checks[0]))});
for(const [name,mutate] of [
 ['empty checks',d=>d.checks=[]],['too many checks',d=>d.checks=Array(65).fill(d.checks[0])],['duplicate key',d=>d.checks.push(d.checks[0])],
 ['unknown status',d=>d.checks[0].status='success'],['wrong detail type',d=>d.checks[0].detail={}],['long detail',d=>d.checks[0].detail='x'.repeat(8193)],
 ['long key',d=>d.checks[0].key='x'.repeat(129)],['empty key',d=>d.checks[0].key=''],['summary hides failure',d=>d.checks[0].status='fail'],
 ['missing write marker',d=>delete d.write_tested],['string write marker',d=>d.write_tested='false'],['write without create',d=>d.write_tested=true],
 ['create hidden as read',d=>d.checks.push({key:'create',status:'ok',detail:''})],['null check',d=>d.checks=[null]],['missing checks',d=>delete d.checks]])
 test(`reject malformed ${name}`,()=>{const d=read();mutate(d);assert.throws(()=>validateReport(d),/capability_invalid/)});
for(const value of [null,undefined,[],true,'report',0])test(`reject ${JSON.stringify(value)}`,()=>assert.throws(()=>validateReport(value),/capability_invalid/));
for(const locale of ['fa','en'])test(`${locale} labels complete`,()=>{const d=JSON.parse(readFileSync(new URL(`../src/admin/locales/${locale}.json`,import.meta.url),'utf8'));for(const key of CHECK_KEYS)assert(d[`cap_check_${key}`]);for(const status of ['ok','warn','fail','skip'])assert(d[`cap_status_${status}`]);});
function lum(hex){const v=hex.match(/[0-9a-f]{2}/gi).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return .2126*v[0]+.7152*v[1]+.0722*v[2]}
for(const [a,b] of [['#172b3a','#ffffff'],['#526576','#ffffff'],['#ffffff','#087785'],['#a72642','#fcecf0'],['#35622f','#e3f1e1'],['#875216','#fbefd9'],['#edf5fa','#192a37'],['#b1c2cf','#192a37'],['#0d3037','#67d6df'],['#ffabc0','#432633'],['#b2dfab','#183a2f'],['#f3cc8e','#413523']])test(`normal text contrast ${a}/${b}`,()=>{const x=lum(a),y=lum(b);assert((Math.max(x,y)+.05)/(Math.min(x,y)+.05)>=4.5)});
const css=readFileSync(new URL('../src/admin/capabilities/capabilities.css',import.meta.url),'utf8');
for(const [a,b] of [['#748594','#ffffff'],['#8197a7','#192a37']])test(`control boundary contrast ${a}/${b}`,()=>{assert(css.includes(a)&&css.includes(b));const x=lum(a),y=lum(b);assert((Math.max(x,y)+.05)/(Math.min(x,y)+.05)>=3)});
test('feature styles scoped to capability dialog',()=>{const clean=css.replace(/\/\*[\s\S]*?\*\//g,'');for(const rule of clean.matchAll(/([^{}]+)\{[^{}]*\}/g)){if(rule[1].trim().startsWith('@'))continue;for(const selector of rule[1].trim().split(','))assert(selector.trim().startsWith('.panel-capabilities '),selector)}});

test('write create failure does not imply cleanup confirmation',()=>{const d={status:'fail',write_tested:true,checks:[{key:'create',status:'fail',detail:'Denied'}]};assert(cleanupUnconfirmed(validateReport(d)));assert(!cleanupUnconfirmed(read()));});
test('successful deletion is the only cleanup confirmation',()=>{const d={status:'ok',write_tested:true,checks:[{key:'create',status:'ok',detail:'pbprobe_ab12cd'},{key:'delete',status:'ok',detail:''}]};assert(!cleanupUnconfirmed(validateReport(d)));});
