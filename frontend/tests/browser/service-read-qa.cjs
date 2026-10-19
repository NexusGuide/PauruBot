/* Optional browser QA: npm run build first; requires Playwright + a local Chromium.
 * Every API response is synthetic. External requests are blocked; no Telegram/provider login.
 * CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/service-read-qa.cjs
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '../../dist');
const cases = [], errors = [];
const service = (id, extra={}) => ({id,label:`Service ${id}`,username:`user_${id}`,status:'active',is_trial:false,data_limit:1024**3,used_bytes:0,expire_at:null,subscription_url:`https://example.test/sub/${id}`,location:null,plan_id:1,panel_id:1,extras:{gb_price:10,day_price:5},delivery:'subscription',changes:['revoke'],...extra});
const profile = locale => ({id:1,telegram_id:1,name:'Synthetic QA',username:'synthetic',locale,balance:100000,currency:'IRT',shop:{title:'Synthetic shop',bot_username:'test_bot',support:null,languages:['en','fa']},trial:{available:false,reason:null,days:1,gb:1},referral:{enabled:false,percent:0,count:0},reseller:{active:false,discount_percent:0},panel_accounts:false,extras:{enabled:true,upgrade:false},payment_methods:['wallet'],topup_bounds:[1,100000],staff:null,preferences:{marketing_opt_out:false,changed_at:null},alerts_muted:false,profile:{phone_masked:null,member_since:'2026-01-01'}});
const deferred = () => {let release;const promise=new Promise(r=>release=r);return {promise,release};};
let browser, server;
async function run() {
 server=http.createServer((req,res)=>{
  const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  let file=path.resolve(root,'.'+pathname);
  if(!file.startsWith(root+path.sep)){res.writeHead(403);return res.end();}
  if(!fs.existsSync(file)||fs.statSync(file).isDirectory())file=path.join(root,'app/index.html');
  res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':file.endsWith('.woff2')?'font/woff2':'application/octet-stream');
  fs.createReadStream(file).pipe(res);
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox']});
 async function scenario(name, options, test) {
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const localErrors=[];page.on('pageerror',e=>localErrors.push(e.message));
  page.setDefaultTimeout(7000);
  let linksCalls=0;
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname!=='127.0.0.1')return route.abort();
   if(!url.pathname.startsWith('/api/'))return route.continue();
   const json=body=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
   const fail=()=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'panel_error'})});
   if(url.pathname==='/api/webapp/me')return json(profile(options.locale||'en'));
   if(url.pathname==='/api/webapp/services')return json({items:[service(1),service(2)]});
   const match=url.pathname.match(/\/services\/(\d+)(.*)/);
   if(match){const id=Number(match[1]),suffix=match[2];
    if(!suffix){if(options.holdDetail&&id===1)await options.holdDetail.promise;if(options.failDetail)return fail();return json({service:service(id,options.serviceExtra||{})});}
    if(suffix==='/links'){linksCalls++;if(options.holdLinks)await options.holdLinks.promise;if(options.failLinks)return fail();return json({links:options.wireguard?['[Interface]\nPrivateKey=synthetic-only\nAddress=10.0.0.2/32']:['vless://synthetic-config']});}
    if(suffix==='/revoke'){if(options.holdRevoke)await options.holdRevoke.promise;return json({service:service(id,{subscription_url:`https://example.test/new/${id}`})});}
    if(suffix==='/auto-renew')return json({service_id:id,revision:'a'.repeat(64),status:'off',rule:null,reason:null,eligible:false,plans:[],gb_options:[],day_options:[],balance:100000,currency:'IRT'});
    if(suffix==='/usage'){const to=new Date().toISOString(),from=new Date(Date.now()-7*86400000).toISOString();return json({service_id:id,days:7,resolution:'hourly_last_observation',from,to,last_observed_at:null,points:[]});}
   }
   return json({items:[]});
  });
  const go = p => page.goto(base+'/app'+p);
  const navigate = p => page.evaluate(p=>{history.pushState(null,'','/app'+p);dispatchEvent(new PopStateEvent('popstate'));},p);
  try {
   await test({page,go,navigate,linksCalls:()=>linksCalls});
   assert.deepEqual(localErrors,[],'no unhandled page errors');cases.push(name);
  }catch(e){errors.push({case:name,error:e.stack});}
  finally{await page.close();}
 }
 const detail={failDetail:true};
 await scenario('detail failure stays on route and retry recovers',detail,async({page,go})=>{
  await go('/services/1');await page.getByRole('alert').waitFor();assert(page.url().endsWith('/app/services/1'));detail.failDetail=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await page.getByRole('heading',{name:'Service 1',exact:true}).waitFor();
 });
 const links={failLinks:true};
 await scenario('config failure shows retry; retry returns configurations',links,async({page,go})=>{
  await go('/services/1');await page.getByRole('button',{name:'Show configs',exact:true}).click();await page.getByRole('alert').waitFor();links.failLinks=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await page.getByRole('button',{name:'vless://synthetic-config',exact:true}).waitFor();
 });
 const holdLinks=deferred();
 await scenario('pending config request disables both config/revoke controls',{holdLinks},async({page,go,linksCalls})=>{
  await go('/services/1');const show=page.getByRole('button',{name:'Show configs',exact:true});await show.click();assert(await show.isDisabled());assert(await page.getByRole('button',{name:'New link',exact:true}).isDisabled());await show.evaluate(el=>el.click());holdLinks.release();await page.getByRole('button',{name:'vless://synthetic-config',exact:true}).waitFor();assert.equal(linksCalls(),1);
 });
 await scenario('QR capacity failure keeps copy fallback and revoke replaces stale QR',{serviceExtra:{subscription_url:'https://example.test/'+ 'x'.repeat(20000)}},async({page,go})=>{
  page.on('dialog',d=>d.accept());await go('/services/1');await page.getByRole('alert').waitFor();assert.equal(await page.locator('button.app-lk').count(),1);await page.getByRole('button',{name:'Try again',exact:true}).click();await page.getByRole('alert').waitFor();await page.getByRole('button',{name:'New link',exact:true}).click();await page.locator('button.app-lk').filter({hasText:'https://example.test/new/1'}).waitFor();await page.getByRole('img',{name:'QR code of the subscription link',exact:true}).waitFor();assert.equal(await page.getByRole('alert').count(),0);
 });
 const wg={wireguard:true,failLinks:true,serviceExtra:{delivery:'wireguard',download_url:'https://example.test/wg.conf'}};
 await scenario('WireGuard QR failure is retryable and download fallback remains',wg,async({page,go})=>{
  await go('/services/1');await page.getByRole('alert').waitFor();assert.equal(await page.getByRole('button',{name:/Download/}).count(),1);wg.failLinks=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await page.getByRole('img',{name:'QR code of the WireGuard configuration',exact:true}).waitFor();
 });
 const extra={failDetail:true};
 await scenario('extra purchase read failure stays on route and retries',extra,async({page,go})=>{
  await go('/extra/1/gb');await page.getByRole('alert').waitFor();assert(page.url().endsWith('/app/extra/1/gb'));extra.failDetail=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await page.getByRole('button').filter({has:page.getByText('5',{exact:true})}).first().waitFor();
 });
 const held=deferred();
 await scenario('late detail for old route cannot replace the next service',{holdDetail:held},async({page,go,navigate})=>{
  await go('/services/1');await navigate('/services/2');await page.getByRole('heading',{name:'Service 2',exact:true}).waitFor();held.release();await page.waitForTimeout(100);assert.equal(await page.getByRole('heading',{name:'Service 1',exact:true}).count(),0);assert.equal(await page.locator('button.app-lk').textContent(),'https://example.test/sub/2');
 });
 const revoke=deferred();
 await scenario('leaving during revoke does not change the next screen or toast success',{holdRevoke:revoke},async({page,go,navigate})=>{
  page.on('dialog',d=>d.accept());await go('/services/1');await page.getByRole('button',{name:'New link',exact:true}).click();await navigate('/services/2');await page.getByRole('heading',{name:'Service 2',exact:true}).waitFor();revoke.release();await page.waitForTimeout(100);assert.equal(await page.locator('button.app-lk').textContent(),'https://example.test/sub/2');assert.equal(await page.getByText('New link created',{exact:true}).count(),0);
 });
 const fa={locale:'fa',failDetail:true};
 await scenario('Persian RTL error and retry',fa,async({page,go})=>{
  await go('/services/1');await page.getByRole('alert').waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');fa.failDetail=false;await page.getByRole('button',{name:'تلاش دوباره',exact:true}).click();await page.getByRole('heading',{name:'Service 1',exact:true}).waitFor();
 });
 console.log(JSON.stringify({passed:cases.length,failed:errors.length,cases,errors},null,2));
 if(errors.length)process.exitCode=1;
}
run().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));});
