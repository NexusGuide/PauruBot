import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source=readFileSync(new URL('../src/app/help/help-data.ts',import.meta.url),'utf8');
const {outputText}=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}});
const {safeHelpUrl,normaliseHelp,matchesHelp,platformApps,validateHelp}=await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const cases=JSON.parse(readFileSync(new URL('../../tests/fixtures/help_urls.json',import.meta.url),'utf8'));
for(const item of cases)test(`URL ${item.allowed?'allowed':'blocked'}: ${item.url.slice(0,80)}`,()=>assert.equal(safeHelpUrl(item.url),item.allowed?item.url:null));
test('non-string links are not navigable',()=>{for(const value of [null,undefined,0,{},[]])assert.equal(safeHelpUrl(value),null)});
test('Persian/Arabic variants, joiners, digits and case are searchable',()=>{assert.equal(normaliseHelp('كاربري ۱۲ ‌ TEST'),'کاربری 12 test');assert(matchesHelp('آموزش اتصال كاربري ۱۲','کاربری 12'));assert(!matchesHelp('Android client','ios'))});
test('query terms use AND, not regex/HTML',()=>{assert(matchesHelp('<script>literal only','literal script'));assert(!matchesHelp('Literal text','.*'));assert(matchesHelp('Any text','  '))});
test('platform filters preserve supplied order and never create defaults',()=>{const items=[{platform:'ios',id:1},{platform:'android',id:2}];assert.deepEqual(platformApps(items,'android'),[items[1]]);assert.deepEqual(platformApps(items,'all'),items);assert.deepEqual(platformApps(items,'unknown'),[])});
const valid=()=>({articles:[{id:0,title:'Guide',text:'Plain text',url:null}],apps:[{id:0,name:'App',platform:'android',store_url:null,release_url:'https://github.com/Owner/App/releases'}],bot_url:null});
test('small contract including empty content is valid',()=>{const d=valid();assert.equal(validateHelp(d),d);assert.deepEqual(validateHelp({articles:[],apps:[],bot_url:null}),{articles:[],apps:[],bot_url:null})});
test('Unicode bounds count code points like server, not UTF-16 units',()=>{const d=valid();d.articles[0].text='😀'.repeat(8000);d.articles[0].title='😀'.repeat(128);d.apps[0].name='😀'.repeat(32);assert.equal(validateHelp(d),d)});
for(const [name,mutate] of [
 ['too many articles',d=>d.articles=Array(31).fill(d.articles[0])],['too many apps',d=>d.apps=Array(21).fill(d.apps[0])],
 ['duplicate article ID',d=>d.articles.push(d.articles[0])],['duplicate app ID',d=>d.apps.push(d.apps[0])],
 ['invalid article ID',d=>d.articles[0].id=-1],['missing article title',d=>d.articles[0].title=''],
 ['wrong article text',d=>d.articles[0].text={}],['long text',d=>d.articles[0].text='x'.repeat(8001)],
 ['unknown platform',d=>d.apps[0].platform='unknown'],['wrong link type',d=>d.apps[0].store_url={}],
 ['wrong bot type',d=>d.bot_url={}],['missing arrays',d=>delete d.articles]])
 test(`malformed response rejected: ${name}`,()=>{const d=valid();mutate(d);assert.throws(()=>validateHelp(d),/help_invalid/)});
