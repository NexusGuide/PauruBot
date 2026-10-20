import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import "./wallet-history.test.mjs";
const {tabForPath,parentForPath}=await import(new URL('../src/app/navigation.ts',import.meta.url));
for (const [path,tab,parent] of [
 ['/', 'home',null],['/buy','buy',null],['/custom','buy','/buy'],['/checkout','buy','/buy'],['/done/12','buy','/services'],
 ['/services','services',null],['/services/12','services','/services'],['/extra/12/gb','services','/services/12'],['/extra/invalid/gb','services','/services'],
 ['/wallet','wallet',null],['/payment/12','wallet','/wallet'],['/more','more',null],['/profile','more','/more'],['/help','more','/more'],
 ['/tickets','more','/more'],['/tickets/new','more','/tickets'],['/tickets/12','more','/tickets'],['/manage','more','/more'],['/desk','more','/more'],['/unknown','home',null]
]) test(`route ${path} has stable tab and safe parent`,()=>{assert.equal(tabForPath(path),tab);assert.equal(parentForPath(path),parent)});
const css=readFileSync(new URL('../src/app/app.css',import.meta.url),'utf8');
function luminance(hex) {const v=hex.match(/[0-9a-f]{2}/gi).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return .2126*v[0]+.7152*v[1]+.0722*v[2]}
function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)}
const blocks=[...css.matchAll(/:root\[data-app="miniapp"\][^{]*\{([^}]+)\}/g)].map(m=>Object.fromEntries([...m[1].matchAll(/--([\w-]+):\s*(#[\da-f]{6})/gi)].map(x=>[x[1],x[2]]))).filter(d=>d.surface);
for(const [i,d] of blocks.entries()) for(const [foreground,background,min] of [['text','surface',4.5],['hint','surface',4.5],['text','bg',4.5],['hint','bg',4.5],['accent-ink','accent',4.5],['accent','accent-soft',4.5],['danger','danger-soft',4.5],['control-line','surface',3]])
 test(`palette ${i}: ${foreground}/${background} >= ${min}`,()=>{assert(contrast(d[foreground],d[background])>=min,`${foreground}/${background}: ${contrast(d[foreground],d[background])}`)});
for(const [name,a,b] of [['light warning','#875216','#fbefd9'],['dark warning','#f3cc8e','#413523'],['dark good','#b2dfab','#183a2f'],['dark brand','#67d6df','#163d47']]) test(`${name} caption contrast`,()=>assert(contrast(a,b)>=4.5));
test('new stylesheet selectors are Mini App scoped',()=>{const clean=css.replace(/\/\*[\s\S]*?\*\//g,'');for(const rule of clean.matchAll(/([^{}]+)\{[^{}]*\}/g)){if(rule[1].trim().startsWith('@'))continue;let depth=0,current='';const selectors=[];for(const ch of rule[1]){if(ch==='(')depth++;if(ch===')')depth--;if(ch===',' && depth===0){selectors.push(current);current='';}else current+=ch;}selectors.push(current);for(const selector of selectors)assert(selector.trim().startsWith(':root[data-app="miniapp"]'),selector)}});
// Render the actual shared Button with tiny hook stubs; no third-party test runner needed.
const {mkdtempSync,writeFileSync,rmSync}=await import('node:fs');
const {pathToFileURL,fileURLToPath}=await import('node:url');
const {execFileSync}=await import('node:child_process');
const temp=mkdtempSync(new URL('./.ux-',import.meta.url));
try {
 let ui=readFileSync(new URL('../src/app/ui.tsx',import.meta.url),'utf8');
 ui=ui.replace(/import \{[^\n]*\} from "react-router-dom";/,'const Link=()=>null; const useLocation=()=>({pathname:"/"}); const useNavigate=()=>()=>{};')
 .replace(/import \{[^\n]*\} from "\.\/navigation";/,'const parentForPath=()=>null; const tabForPath=()=>"home";')
 .replace(/import \{[^\n]*\} from "\.\/state";/,'const useApp=()=>({t:(key:string)=>key,toastMessage:null}); const errorText=()=>"error";');
 writeFileSync(temp+'/ui.tsx',ui);
 execFileSync(process.execPath,[
  fileURLToPath(new URL('../node_modules/typescript/lib/tsc.js',import.meta.url)),
  '--ignoreConfig','--noCheck','--jsx','react-jsx','--module','esnext','--target','es2022','--outDir',temp,temp+'/ui.tsx',
 ],{stdio:'pipe'});
 const {Button}=await import(pathToFileURL(temp+'/ui.js'));
 const {createElement}=await import('react'); const {renderToStaticMarkup}=await import('react-dom/server');
 for(const [busy,disabled,wanted] of [[true,false,true],[true,true,true],[false,true,true],[false,false,false],[undefined,undefined,false]])
 test(`Button busy=${busy} disabled=${disabled}`,()=>{const html=renderToStaticMarkup(createElement(Button,{busy,disabled},'Submit ticket'));assert.equal(html.includes('disabled=""'),wanted);assert(html.includes('Submit ticket'));assert.equal(html.includes('aria-busy="true"'),!!busy);if(busy){assert(html.includes('aria-hidden="true"'));assert(!html.includes('role="status"'))}});
 test('Button keeps explicit submit type and caller class',()=>{const html=renderToStaticMarkup(createElement(Button,{type:'submit',className:'custom'},'Save'));assert(html.includes('type="submit"'));assert(html.includes('custom'))});
} finally {rmSync(temp,{recursive:true,force:true})}

// Customer ticket filter regressions share the existing CI UX suite.
import "./ticket-pages.test.mjs";

// Service detail recovery regressions run in the existing CI UX suite.
import "./service-read.test.mjs";

// Bounded service search/paging contracts run in the existing CI UX suite.
import "./service-pages.test.mjs";
