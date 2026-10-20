/** Optional, synthetic screen integration QA. No real credentials or external network.
 * Requires external esbuild + playwright + Chromium; not a production/CI dependency.
 * Real screen, state, API client, HTTP client, hooks, UI, localization and router are bundled.
 * Only unrelated child widgets and Payment's memo helpers are replaced, explicitly below.
 */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { build } = require(process.env.ESBUILD_MODULE || 'esbuild');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const defer = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
const profile = (locale = 'en', id = 1) => ({ id, telegram_id: 100 + id, name: 'Synthetic user', username: 'synthetic', locale,
 balance: 500000, currency: 'IRT', shop: { title: 'Synthetic shop', bot_username: 'synthetic_bot', support: null, languages: ['fa','en'] },
 trial: { available: false, reason: 'trial_disabled', days: 1, gb: 1 }, referral: { enabled: false, percent: 0, count: 0 },
 reseller: { active: false, discount_percent: 0 }, panel_accounts: false, extras: { enabled: false, gb_price: 0, day_price: 0, upgrade: false },
 payment_methods: ['card'], topup_bounds: [10000,1000000], staff: null, preferences: { marketing_opt_out: true, changed_at: null },
 alerts_muted: false, profile: { phone_masked: null, member_since: '2026-01-01T00:00:00Z' } });
const service = (id = 7, label = 'Original') => ({ id, label, username: `fixed_${id}`, status: 'active', is_trial: false, data_limit: 1000000000,
 used_bytes: 100, expire_at: null, subscription_url: 'https://subscription.invalid/synthetic', location: null, plan_id: 1, panel_id: 1,
 extras: null, delivery: 'subscription', changes: ['revoke'] });
const transaction = (id = 9, amount = 25000) => ({ id, amount, balance_after: 500000, kind: 'topup', created_at: '2026-10-18T00:00:00Z' });
async function run(mode, cases) {
 const root = process.env.SOURCE_ROOT || path.resolve(__dirname, '../..');
 const react = path.dirname(require.resolve('react/package.json', { paths: [root] }));
 const screen = mode === 'wallet' ? 'Wallet' : 'Services';
 const entry = `import React from 'react'; import {createRoot} from 'react-dom/client';
 import {BrowserRouter,Routes,Route,useNavigate} from 'react-router-dom';
 import {AppProvider,useApp} from './src/app/state'; import {Toast} from './src/app/ui';
 import {${mode === 'wallet' ? 'Wallet' : 'ServiceDetail'}} from './src/app/screens/${screen}';
 function Controller(){const n=useNavigate(),a=useApp(); React.useEffect(()=>{window.qaGo=n;window.qaReload=a.reload},[n,a.reload]);return null}
 createRoot(document.getElementById('root')).render(<AppProvider><BrowserRouter><Controller/><Toast/><Routes>
 <Route path="${mode === 'wallet' ? '/wallet' : '/services/:id'}" element={<${mode === 'wallet' ? 'Wallet' : 'ServiceDetail'}/>}/>
 <Route path="*" element={<h1>Other route</h1>}/></Routes></BrowserRouter></AppProvider>);`;
 const stubs = {
  './Home': 'export const timeRatio=()=>0.5;export const usageRatio=()=>0.5;',
  '../autoRenew/AutoRenew': 'export const AutoRenew=()=>null;',
  '../usage/UsageChart': 'export const UsageChart=()=>null;',
  './Payment': 'export const rememberCard=()=>{};export const rememberPayment=()=>{};',
 };
 const output = await build({ stdin: { contents: entry, loader: 'tsx', resolveDir: root }, bundle: true, write: false,
  jsx: 'automatic', platform: 'browser', format: 'iife', define: { 'process.env.NODE_ENV': '"production"' },
  alias: { react, '@shared': path.join(root, 'src/shared') },
  plugins: [{ name: 'unrelated-widget-fixtures', setup(b) {
   b.onResolve({ filter: /^\.\.?\// }, args => Object.hasOwn(stubs,args.path) && args.importer.endsWith(`/screens/${screen}.tsx`) ? { path: args.path, namespace: 'qa-widget' } : undefined);
   b.onLoad({ filter: /.*/, namespace: 'qa-widget' }, args => ({ contents: stubs[args.path], loader: 'js' }));
  } }], logLevel: 'silent' });
 const js = output.outputFiles[0].text;
 const css = fs.readFileSync(path.join(root,'src/app/app.css'),'utf8');
 const server = http.createServer((req,res) => {
  if (req.url === '/main.js') { res.setHeader('content-type','text/javascript'); res.end(js); }
  else { res.setHeader('content-type','text/html'); res.end(`<html data-app="miniapp"><head><meta name="viewport" content="width=device-width"><style>*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial}button,input{font:inherit} ${css}</style></head><body><div id="root"></div><script>window.Telegram={WebApp:{initData:'synthetic-not-a-credential',HapticFeedback:{notificationOccurred:x=>(window.feedback??=[]).push(x),selectionChanged:()=>{}}}}</script><script src="/main.js"></script></body></html>`); }
 });
 let browser;
 const results = [];
 try {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve)); const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  for (const [name, scenario] of cases) {
   const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
   const page = await context.newPage(); page.setDefaultTimeout(2500);
   const errors = [], requests = [], unexpected = [];
   page.on('pageerror',error => errors.push(error.message));
   const state = { profile: profile(), history: async () => ({ items: [] }), rename: async (id, body) => ({ service: service(id,body.label) }), gift: async () => ({ amount: 1000,balance:501000 }) };
   const fulfill = async (route, data) => { try { await route.fulfill({ status: data?.status ?? 200, contentType: 'application/json', body: JSON.stringify(data?.status ? data.body : data) }); } catch (err) { if (!page.isClosed()) throw err; } };
   await page.route('**/*',async route => {
    const req=route.request(),u=new URL(req.url());
    if(u.origin!==origin){unexpected.push(req.url());return route.abort();}
    if(!u.pathname.startsWith('/api/'))return route.continue();
    requests.push({ path:u.pathname,method:req.method(),body:req.postDataJSON() });
    if(req.headers()['x-telegram-init-data']!=='synthetic-not-a-credential')unexpected.push('missing signed-principal header fixture');
    if(u.pathname==='/api/webapp/me')return fulfill(route,state.profile);
    if(u.pathname==='/api/webapp/wallet/history')return fulfill(route,await state.history());
    if(u.pathname==='/api/webapp/wallet/gift')return fulfill(route,await state.gift());
    const m=u.pathname.match(/^\/api\/webapp\/services\/(\d+)(?:\/(rename|links|revoke))?$/);
    if(m){const id=Number(m[1]);if(m[2]==='rename')return fulfill(route,await state.rename(id,req.postDataJSON()));
     if(m[2]==='links')return fulfill(route,{links:[]});return fulfill(route,{service:service(id)});}
    if(u.pathname==='/api/webapp/wallet/topup')return fulfill(route,{status:503,body:{error:'gateway_error'}});
    unexpected.push(u.pathname);return fulfill(route,{status:500,body:{error:'unexpected_qa_request'}});
   });
   const go = () => page.goto(`${origin}${mode==='wallet'?'/wallet':'/services/7'}`);
   try {
    await scenario({ page,state,requests,go,defer,sleep,assert,profile,service,transaction });
    assert.deepEqual(errors,[],'unhandled browser errors');assert.deepEqual(unexpected,[],'external or unexpected request');
    if(process.env.QA_SCREENSHOT && name.includes('Persian RTL'))await page.screenshot({path:process.env.QA_SCREENSHOT,fullPage:true});
    results.push({name,status:'pass',requests});console.log(`PASS ${name}`);
   } catch(error) { results.push({name,status:'fail',error:error.message,errors,requests,unexpected});console.log(`FAIL ${name}: ${error.message.split('\n')[0]}`); }
   finally { await context.close(); }
  }
 } finally { if(browser)await browser.close();await new Promise(resolve=>server.close(resolve)); }
 const summary={ mode,sourceRoot:root,tests:results.length,passed:results.filter(x=>x.status==='pass').length,failed:results.filter(x=>x.status==='fail').length,results };
 if(process.env.QA_OUTPUT)fs.writeFileSync(process.env.QA_OUTPUT,JSON.stringify(summary,null,2));
 console.log(JSON.stringify({tests:summary.tests,passed:summary.passed,failed:summary.failed}));
 if(summary.failed)throw new Error(`${summary.failed} synthetic browser scenarios failed`);
}
module.exports={run};
