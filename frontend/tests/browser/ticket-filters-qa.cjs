/* Optional browser QA: npm run build first; requires Playwright + a local Chromium.
 * Every API response is synthetic. External requests are blocked; no Telegram/provider login.
 * CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/ticket-filters-qa.cjs
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '../../dist');
const cases = [], errors = [];
const profile = locale => ({id:1,telegram_id:1,name:'Synthetic QA',username:'synthetic',locale,balance:100000,currency:'IRT',shop:{title:'Synthetic shop',bot_username:'test_bot',support:null,languages:['en','fa']},trial:{available:false,reason:null,days:1,gb:1},referral:{enabled:false,percent:0,count:0},reseller:{active:false,discount_percent:0},panel_accounts:false,extras:{enabled:true,upgrade:false},payment_methods:['wallet'],topup_bounds:[1,100000],staff:null,preferences:{marketing_opt_out:false,changed_at:null},alerts_muted:false,profile:{phone_masked:null,member_since:'2026-01-01'}});
const deferred = () => {let release;const promise=new Promise(r=>release=r);return {promise,release};};
let browser,server;
const ticket=(id)=>({id,subject:`Ticket ${id}`,status:id<=2?'closed':id===3?'answered':'open',department_id:id===1?2:id===2?null:1,created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'});
const desks=[{id:1,title:'Technical'}],historyDesks=[...desks,{id:2,title:'Retired desk'}];
const waitUntil=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,20));}throw Error('mock request not observed');};
async function run(){
 server=http.createServer((req,res)=>{
  const pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);
  let file=path.resolve(root,'.'+pathname);
  if(!file.startsWith(root+path.sep)){res.writeHead(403);return res.end();}
  if(!fs.existsSync(file)||fs.statSync(file).isDirectory())file=path.join(root,'app/index.html');
  res.setHeader('content-type',file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream');
  fs.createReadStream(file).pipe(res);
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base=`http://127.0.0.1:${server.address().port}`;
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox']});
 async function scenario(name,options,test){
  const page=await browser.newPage({viewport:{width:390,height:844}});page.setDefaultTimeout(7000);
  const calls=[],pageErrors=[];page.on('pageerror',e=>pageErrors.push(e.message));
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());if(url.hostname!=='127.0.0.1')return route.abort();
   if(!url.pathname.startsWith('/api/'))return route.continue();
   const json=body=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
   if(url.pathname==='/api/webapp/me')return json(profile(options.locale||'en'));
   if(url.pathname==='/api/webapp/tickets'){
    const status=url.searchParams.get('status')||'',dep=url.searchParams.get('department_id'),before=Number(url.searchParams.get('before')||0);
    calls.push({status,dep,before});if(options.holdStatus?.[status])await options.holdStatus[status].promise;
    if(options.fail || options.failBefore===before)return route.fulfill({status:503,contentType:'application/json',body:'{"error":"panel_error"}'});
    let items=options.empty?[]:Array.from({length:47},(_,i)=>ticket(47-i));
    items=items.filter(t=>(!status||t.status===status)&&(dep===null||(dep==='0'?t.department_id===null:t.department_id===Number(dep)))&&(!before||t.id<before));
    const visible=items.slice(0,20),next_before=items.length>20?visible.at(-1).id:null;
    return json({items:options.wrongStatus?visible.map(t=>({...t,status:"open"})):visible,next_before:options.malformed?0:next_before,departments:options.noDesks?[]:desks,filter_departments:options.legacy?undefined:options.noDesks?[]:historyDesks});
   }
   const match=url.pathname.match(/^\/api\/webapp\/tickets\/(\d+)$/);
   if(match)return json({ticket:ticket(Number(match[1])),messages:[],next_before:null});
   return json({items:[]});
  });
  const go=p=>page.goto(base+'/app'+p),navigate=p=>page.evaluate(p=>{history.pushState(null,'','/app'+p);dispatchEvent(new PopStateEvent('popstate'));},p);
  const status=()=>page.getByRole('group',{name:'Status',exact:true});
  const department=()=>page.getByRole('group',{name:'Department',exact:true});
  const rows=()=>page.getByRole('button',{name:/^#\d+ — Ticket /});
  const count=async n=>{await page.waitForFunction(n=>[...document.querySelectorAll('main button')].filter(b=>/^#\d+ — Ticket /.test(b.textContent)).length===n,n);};
  try{await test({page,go,navigate,calls,status,department,rows,count});assert.deepEqual(pageErrors,[]);cases.push(name);}
  catch(e){errors.push({case:name,error:e.stack,url:page.url(),calls});}
  finally{await page.close();}
 }
 await scenario('status reaches older matching tickets beyond the first unfiltered page',{},async({go,status,count,rows,calls})=>{
  await go('/tickets');await count(20);await status().getByRole('button',{name:'Closed',exact:true}).click();await count(2);assert.equal(await rows().first().textContent().then(s=>s.includes('#2')),true);assert.deepEqual(calls.at(-1),{status:'closed',dep:null,before:0});
 });
 await scenario('department combines globally with status including retired desk',{},async({go,status,department,count,calls})=>{
  await go('/tickets');await count(20);await department().getByRole('button',{name:'Retired desk',exact:true}).click();await count(1);await status().getByRole('button',{name:'Closed',exact:true}).click();await count(1);await waitUntil(()=>calls.at(-1).status==='closed');assert.equal(calls.at(-1).dep,'2');assert.equal(calls.at(-1).before,0);
 });
 await scenario('General support is distinct from all departments',{},async({go,department,count,calls})=>{
  await go('/tickets');await count(20);await department().getByRole('button',{name:'General support',exact:true}).click();await count(1);assert.equal(calls.at(-1).dep,'0');await department().getByRole('button',{name:'All',exact:true}).click();await count(20);assert.equal(calls.at(-1).dep,null);
 });
 await scenario('filter changes reset old cursor and Newest retains filters',{},async({page,go,status,count,calls})=>{
  await go('/tickets');await count(20);await page.getByRole('button',{name:'Older tickets',exact:true}).click();await count(20);await waitUntil(()=>calls.at(-1).before===28);await status().getByRole('button',{name:'Awaiting support',exact:true}).click();await waitUntil(()=>calls.at(-1).status==='open');assert.equal(calls.at(-1).before,0);await page.getByRole('button',{name:'Older tickets',exact:true}).click();await waitUntil(()=>calls.at(-1).before===28);await page.getByRole('button',{name:'Newest tickets',exact:true}).click();await waitUntil(()=>calls.at(-1).before===0);assert.equal(calls.at(-1).status,'open');
 });
 const failure={fail:true};
 await scenario('initial failure is retryable and not an empty result',failure,async({page,go,count})=>{
  await go('/tickets?status=closed');await page.getByRole('alert').waitFor();assert.equal(await page.getByText('No tickets match these filters on this page.',{exact:true}).count(),0);failure.fail=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await count(2);
 });
 const olderFailure={failBefore:28};
 await scenario('older-page failure retries the same filter/cursor and can return to newest',olderFailure,async({page,go,count,calls})=>{
  await go('/tickets?status=open');await count(20);await page.getByRole('button',{name:'Older tickets',exact:true}).click();await page.getByRole('alert').waitFor();assert.equal(calls.at(-1).before,28);olderFailure.failBefore=-1;await page.getByRole('button',{name:'Try again',exact:true}).click();await count(20);assert.deepEqual(calls.slice(-2).map(c=>c.before),[28,28]);await page.getByRole('button',{name:'Newest tickets',exact:true}).click();await waitUntil(()=>calls.at(-1).before===0);
 });
 const old=deferred();
 await scenario('late old filter cannot replace the next result', {holdStatus:{closed:old}},async({go,status,count,calls,rows,page})=>{
  await go('/tickets');await count(20);await status().getByRole('button',{name:'Closed',exact:true}).click();await waitUntil(()=>calls.some(c=>c.status==='closed'));await status().getByRole('button',{name:'Support replied',exact:true}).click();await count(1);old.release();await page.waitForTimeout(100);await count(1);assert((await rows().textContent()).includes('#3'));
 });
 await scenario('thread in-app Back restores filters and current cursor',{},async({page,go,count,rows,calls})=>{
  await go('/tickets?status=open&department_id=1&before=28');await count(20);await rows().first().click();await page.getByRole('heading',{name:'Ticket 27',exact:true}).waitFor();await page.getByRole('button',{name:'Back to parent page',exact:true}).click();await count(20);assert.deepEqual(calls.at(-1),{status:'open',dep:'1',before:28});
 });
 await scenario('browser Back restores filter route',{},async({page,go,count,rows,calls})=>{
  await go('/tickets?status=closed&department_id=2');await count(1);await rows().click();await page.getByRole('heading',{name:'Ticket 1',exact:true}).waitFor();await page.goBack();await count(1);assert.deepEqual(calls.at(-1),{status:'closed',dep:'2',before:0});
 });
 await scenario('filtered empty page keeps filter controls and clear recovery',{},async({page,go,count})=>{
  await go('/tickets?status=answered&department_id=0');await page.getByText('No tickets match these filters on this page.',{exact:true}).waitFor();await page.getByRole('button',{name:'Retired desk',exact:true}).waitFor();await page.getByRole('button',{name:'Clear ticket filters',exact:true}).click();await count(20);
 });
 const bad={malformed:true};
 await scenario('malformed response is a visible retryable error',bad,async({page,go,count})=>{
  await go('/tickets');await page.getByRole('alert').waitFor();bad.malformed=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await count(20);
 });
 await scenario('new ticket choices exclude retired history departments',{},async({page,go})=>{
  await go('/tickets/new');await page.getByRole('button',{name:'Technical',exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'Retired desk',exact:true}).count(),0);
 });
 await scenario('General filter remains available when no configured departments exist',{noDesks:true},async({go,department,count,calls})=>{
  await go('/tickets');await count(20);await department().getByRole('button',{name:'General support',exact:true}).click();await count(1);assert.equal(calls.at(-1).dep,'0');
 });
 await scenario('Persian RTL filters and filtered-empty copy',{locale:'fa'},async({page,go})=>{
  await go('/tickets?status=answered&department_id=0');await page.getByText('در این صفحه تیکتی مطابق این فیلترها نیست.',{exact:true}).waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');await page.getByRole('button',{name:'پاک کردن فیلترهای تیکت',exact:true}).waitFor();
 });
 await scenario('external same-screen navigation restores the status and cursor',{},async({go,navigate,status,count,calls})=>{
  await go('/tickets?status=closed');await count(2);await navigate('/tickets?status=open&before=28');await count(20);assert.equal(await status().getByRole('button',{name:'Awaiting support',exact:true}).getAttribute('aria-pressed'),'true');assert.equal(calls.at(-1).before,28);
 });
 const legacy={legacy:true};
 await scenario('legacy backend without filter contract fails visibly instead of false success',legacy,async({page,go,count})=>{
  await go('/tickets?status=closed');await page.getByRole('alert').waitFor();assert.equal(await page.getByText('No tickets match these filters on this page.',{exact:true}).count(),0);legacy.legacy=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await count(2);
 });
 const mismatch={wrongStatus:true};
 await scenario('response rows inconsistent with filter are rejected and can retry',mismatch,async({page,go,count})=>{
  await go('/tickets?status=closed');await page.getByRole('alert').waitFor();mismatch.wrongStatus=false;await page.getByRole('button',{name:'Try again',exact:true}).click();await count(2);
 });
 console.log(JSON.stringify({passed:cases.length,failed:errors.length,cases,errors},null,2));if(errors.length)process.exitCode=1;
}
run().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve));});
