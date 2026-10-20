/** Optional, synthetic screen integration QA. No real credentials or external network.
 * Requires external esbuild + playwright + Chromium; not a production/CI dependency.
 * Real screen, state, API client, HTTP client, hooks, UI, localization and router are bundled.
 * Only unrelated child widgets and Payment's memo helpers are replaced, explicitly below.
 */
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
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
 alerts_muted: false, alert_policy: {warn_remaining_percent:10,warn_days:2}, profile: { phone_masked: null, member_since: '2026-01-01T00:00:00Z' } });
const service = (id = 7, label = 'Original') => ({ id, label, username: `fixed_${id}`, status: 'active', is_trial: false, data_limit: 1000000000,
 used_bytes: 100, expire_at: null, subscription_url: 'https://subscription.invalid/synthetic', location: null, plan_id: 1, panel_id: 1,
 extras: null, delivery: 'subscription', changes: ['revoke'] });
const transaction = (id = 9, amount = 25000) => ({ id, amount, balance_after: 500000, kind: 'topup', created_at: '2026-10-18T00:00:00Z' });
async function run(mode, cases) {
 const root = process.env.SOURCE_ROOT || path.resolve(__dirname, '../..');
 const react = path.dirname(require.resolve('react/package.json', { paths: [root] }));
 const screen = 'Notifications';
 const entry = `import React from 'react'; import {createRoot} from 'react-dom/client';
 import {BrowserRouter,Routes,Route,useNavigate} from 'react-router-dom';
 import {AppProvider,useApp} from './src/app/state'; import {Toast} from './src/app/ui';
 import {Notifications} from './src/app/screens/Notifications'; import {Home} from './src/app/screens/Home';
 function Gate(){const {me}=useApp();return me?<Routes><Route path='/notifications' element={<Notifications/>}/><Route path='/home' element={<Home/>}/><Route path='*' element={<h1>Other route</h1>}/></Routes>:null} function Controller(){const n=useNavigate(),a=useApp(); React.useEffect(()=>{window.qaGo=n;window.qaReload=a.reload},[n,a.reload]);return null}
 createRoot(document.getElementById('root')).render(<AppProvider><BrowserRouter><Controller/><Toast/><Gate/></BrowserRouter></AppProvider>);`;
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
  browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || execFileSync('which',['chromium'],{encoding:'utf8'}).trim(), headless: true, args: ['--no-sandbox'] });
  for (const [name, scenario] of cases) {
   const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
   await context.addInitScript(now=>{Date.now=()=>now},Date.parse('2026-10-18T00:00:00Z'));
   const page = await context.newPage(); page.setDefaultTimeout(2500);
   const errors = [], requests = [], unexpected = [];
   page.on('pageerror',error => errors.push(error.message));
   const state = { profile: profile(), history: async () => ({ items: [] }), services: async()=>({items:[]}), rename: async (id, body) => ({ service: service(id,body.label) }), gift: async () => ({ amount: 1000,balance:501000 }) };
   const fulfill = async (route, data) => { try { await route.fulfill({ status: data?.status ?? 200, contentType: 'application/json', body: JSON.stringify(data?.status ? data.body : data) }); } catch (err) { if (!page.isClosed()) throw err; } };
   await page.route('**/*',async route => {
    const req=route.request(),u=new URL(req.url());
    if(u.origin!==origin){unexpected.push(req.url());return route.abort();}
    if(!u.pathname.startsWith('/api/'))return route.continue();
    requests.push({ path:u.pathname,method:req.method(),body:req.postDataJSON() });
    if(req.headers()['x-telegram-init-data']!=='synthetic-not-a-credential')unexpected.push('missing signed-principal header fixture');
    if(u.pathname==='/api/webapp/me')return fulfill(route,state.profile);
    if(u.pathname==='/api/webapp/services')return fulfill(route,await state.services());
    if(u.pathname==='/api/webapp/wallet/history')return fulfill(route,await state.history());
    if(u.pathname==='/api/webapp/wallet/gift')return fulfill(route,await state.gift());
    const m=u.pathname.match(/^\/api\/webapp\/services\/(\d+)(?:\/(rename|links|revoke))?$/);
    if(m){const id=Number(m[1]);if(m[2]==='rename')return fulfill(route,await state.rename(id,req.postDataJSON()));
     if(m[2]==='links')return fulfill(route,{links:[]});return fulfill(route,{service:service(id)});}
    if(u.pathname==='/api/webapp/wallet/topup')return fulfill(route,{status:503,body:{error:'gateway_error'}});
    unexpected.push(u.pathname);return fulfill(route,{status:500,body:{error:'unexpected_qa_request'}});
   });
   const go = (path='/notifications') => page.goto(`${origin}${path}`);
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


const NOW=Date.parse('2026-10-18T00:00:00Z');
const en=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../src/app/locales/en.json'),'utf8'));
const fa=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../src/app/locales/fa.json'),'utf8'));
const near=(service,{id=7,remaining=12,days=2.5}={})=>({...service(id),data_limit:100,used_bytes:100-remaining,expire_at:new Date(NOW+days*86400000).toISOString()});
const fixture=(state,service,options)=>{state.services=async()=>({items:[near(service,options)]})};
const title=(page,key)=>page.getByText(en[key],{exact:true});
run('alert-policy',[
 ['shop defaults suppress old hard-coded warnings on Home and Notifications',async({page,state,go,service,assert})=>{
  fixture(state,service);await go('/home');await page.getByText('Original',{exact:true}).waitFor();
  assert.equal(await page.locator('.app-prog').getAttribute('data-tone'),null);
  const bell=page.getByRole('button',{name:'Notifications',exact:true});assert.equal(await bell.locator('span[aria-hidden=true]').count(),0);
  await bell.click();await title(page,'notif_empty').waitFor();assert.equal(await title(page,'alert_low_data_title').count(),0);assert.equal(await title(page,'alert_expiring_title').count(),0);
 }],
 ['custom shop thresholds drive feed, badge and Home progress cue',async({page,state,go,service,assert})=>{
  state.profile.alert_policy={warn_remaining_percent:30,warn_days:4};fixture(state,service,{remaining:20,days:3.5});await go('/home');await page.getByText('Original',{exact:true}).waitFor();
  assert.equal(await page.locator('.app-prog').getAttribute('data-tone'),'orange');const bell=page.getByRole('button',{name:'Notifications',exact:true});
  await bell.locator('span[aria-hidden=true]').waitFor({state:'attached'});assert.equal(await bell.locator('span[aria-hidden=true]').count(),1);await bell.click();await title(page,'alert_low_data_title').waitFor();await title(page,'alert_expiring_title').waitFor();
 }],
 ['inclusive percent and exact expiry boundary match worker thresholds',async({page,state,go,service})=>{
  fixture(state,service,{remaining:10,days:2});await go();await title(page,'alert_low_data_title').waitFor();await title(page,'alert_expiring_title').waitFor();
 }],
 ['zero thresholds retain terminal statuses and wallet activity without predictive warning',async({page,state,go,service,transaction,assert})=>{
  state.profile.alert_policy={warn_remaining_percent:0,warn_days:0};state.services=async()=>({items:[near(service,{remaining:10,days:1}),{...near(service,{id:8}),status:'limited',used_bytes:100,expire_at:null},near(service,{id:9,remaining:100,days:-1})]});state.history=async()=>({items:[transaction()]});await go();
  await title(page,'alert_data_title').waitFor();await title(page,'alert_expired_title').waitFor();await title(page,'tx_topup').waitFor();assert.equal(await title(page,'alert_low_data_title').count(),0);assert.equal(await title(page,'alert_expiring_title').count(),0);
 }],
 ...[['missing',undefined],['malformed',{warn_remaining_percent:'10',warn_days:2}]].map(([name,value])=>[
  name+' policy is explicit, with no fabricated fallback',async({page,state,go,service,transaction,assert})=>{
   if(value===undefined)delete state.profile.alert_policy;else state.profile.alert_policy=value;fixture(state,service);state.history=async()=>({items:[transaction()]});await go();
   await title(page,'alerts_policy_unavailable').waitFor();await title(page,'tx_topup').waitFor();assert.equal(await title(page,'alert_low_data_title').count(),0);assert.equal(await title(page,'alert_expiring_title').count(),0);
  }
 ]),
 ['marketing opt-out and Telegram alert mute are not rewritten or conflated with local feed',async({page,state,go,service,requests,assert})=>{
  state.profile.alert_policy={warn_remaining_percent:30,warn_days:4};state.profile.alerts_muted=true;state.profile.preferences.marketing_opt_out=true;fixture(state,service);await go();
  await title(page,'alert_low_data_title').waitFor();await title(page,'alert_expiring_title').waitFor();assert.equal(requests.filter(x=>x.method==='POST').length,0);
 }],
 ['same-customer profile refresh updates thresholds without a financial action',async({page,state,go,service,requests,assert})=>{
  fixture(state,service);await go();await title(page,'notif_empty').waitFor();state.profile.alert_policy={warn_remaining_percent:30,warn_days:4};await page.evaluate(()=>window.qaReload());
  await title(page,'alert_low_data_title').waitFor();await title(page,'alert_expiring_title').waitFor();assert.equal(requests.filter(x=>x.method==='POST').length,0);
 }],
 ['owner change cannot retain old pending service/history responses',async({page,state,go,service,transaction,profile,defer,sleep,assert})=>{
  const old=defer();let n=0;state.services=()=>++n===1?old.promise:Promise.resolve({items:[]});state.history=async()=>({items:[transaction(30,98765)]});await go();
  await page.waitForFunction(()=>typeof window.qaReload==='function');await page.waitForTimeout(100);state.profile=profile('en',2);await page.evaluate(()=>window.qaReload());
  await page.getByText(/98,765/).waitFor();old.resolve({items:[{...near(service,{remaining:1,days:1}),label:'Old owner only'}]});await sleep(100);
  assert.equal(await page.getByText(/Old owner only/).count(),0);assert.equal(await title(page,'alert_low_data_title').count(),0);
 }],
 ['Home read subtree also resets on confirmed owner change',async({page,state,go,service,profile,defer,sleep,assert})=>{
  const old=defer();let n=0;state.services=()=>++n===1?old.promise:Promise.resolve({items:[{...service(80),label:'New owner only'}]});await go('/home');
  await page.getByRole('button',{name:'Notifications',exact:true}).waitFor();state.profile=profile('en',2);await page.evaluate(()=>window.qaReload());
  await page.getByText('New owner only',{exact:true}).waitFor();old.resolve({items:[{...service(70),label:'Old owner only'}]});await sleep(100);assert.equal(await page.getByText('Old owner only',{exact:true}).count(),0);
 }],
 ['read failure retries GETs only and then applies current policy',async({page,state,go,service,requests,assert})=>{
  let n=0;state.profile.alert_policy={warn_remaining_percent:30,warn_days:4};state.services=async()=>++n===1?{status:503,body:{error:'gateway_error'}}:{items:[near(service)]};await go();
  await page.getByRole('button',{name:'Try again',exact:true}).click();await title(page,'alert_low_data_title').waitFor();assert.equal(n,2);assert.equal(requests.filter(x=>x.method==='POST').length,0);
 }],
 ['Persian RTL fallback is explicit and localized',async({page,state,go,service,assert})=>{
  state.profile.locale='fa';delete state.profile.alert_policy;fixture(state,service);await go();await page.getByText(fa.alerts_policy_unavailable,{exact:true}).waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');
 }],
]).catch(e=>{console.error(e.message);process.exitCode=1});
