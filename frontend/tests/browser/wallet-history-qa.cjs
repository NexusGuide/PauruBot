const {run}=require('./screen-browser.cjs');
const region=p=>p.getByRole('region',{name:'Transactions',exact:true});
const retry=p=>region(p).getByRole('button',{name:'Try again',exact:true});
run('wallet',[
 ['history loading is explicit, without claiming empty success',async({page,state,go,defer,assert})=>{
  const pending=defer();state.history=()=>pending.promise;await go();await region(page).getByRole('status').waitFor();
  assert.equal(await region(page).getAttribute('aria-busy'),'true');assert.equal(await page.getByText('No transactions yet.',{exact:true}).count(),0);pending.resolve({items:[]});
 }],
 ['failed history retries only the read and recovers',async({page,state,requests,go,assert,transaction})=>{
  let n=0;state.history=async()=>++n===1?{status:503,body:{error:'gateway_error'}}:{items:[transaction()]};await go();await retry(page).click();
  await region(page).getByText('Top-up',{exact:true}).waitFor();assert.equal(n,2);assert.equal(requests.filter(x=>x.method==='POST').length,0);
 }],
 ['confirmed empty history has a distinct state',async({page,go,assert})=>{
  await go();await page.getByText('No transactions yet.',{exact:true}).waitFor();assert.equal(await page.getByRole('alert').count(),0);
 }],
 ['gift success survives failed history refresh without replaying gift',async({page,state,requests,go,assert,transaction})=>{
  let n=0;state.history=async()=>++n===2?{status:503,body:{error:'gateway_error'}}:{items:n===1?[]:[transaction()]};await go();
  await page.getByText('No transactions yet.',{exact:true}).waitFor();await page.getByRole('textbox',{name:'Gift code',exact:true}).fill('SYNTHETIC');
  await page.getByRole('button',{name:'Redeem',exact:true}).click();await retry(page).waitFor();await page.getByText('1,000 Toman added to your wallet.',{exact:true}).waitFor();
  await retry(page).click();await region(page).getByText('Top-up',{exact:true}).waitFor();assert.equal(requests.filter(x=>x.path.endsWith('/gift')).length,1);
 }],
 ['owner switch cannot reuse the previous owner history response',async({page,state,go,defer,assert,transaction,profile,sleep})=>{
  const old=defer();let n=0;state.history=()=>++n===1?old.promise:Promise.resolve({items:[transaction(20,98765)]});await go();
  await page.getByText('Wallet balance',{exact:true}).waitFor();state.profile=profile('en',2);await page.evaluate(()=>window.qaReload());
  await region(page).getByText('+98,765',{exact:true}).waitFor();old.resolve({items:[transaction(9,12345)]});await sleep(100);
  assert.equal(await region(page).getByText('+12,345',{exact:true}).count(),0);
 }],
 ['leaving during failed history does not create an unhandled rejection',async({page,state,go,defer,sleep})=>{
  const old=defer();state.history=()=>old.promise;await go();await page.getByText('Wallet balance',{exact:true}).waitFor();
  await page.evaluate(()=>window.qaGo('/other'));await page.getByText('Other route',{exact:true}).waitFor();old.resolve({status:503,body:{error:'gateway_error'}});await sleep(100);
 }],
 ['read retry preserves PR96 uncertain top-up key and does not resend payment',async({page,state,requests,go,assert})=>{
  let n=0;state.history=async()=>++n===1?{status:503,body:{error:'gateway_error'}}:{items:[]};await go();
  const pay=page.getByRole('button',{name:/^Card transfer/});await pay.click();await pay.waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('.app-toast')?.textContent.includes("gateway"));
  await retry(page).click();await region(page).getByText('No transactions yet.',{exact:true}).waitFor();
  assert.equal(requests.filter(x=>x.path.endsWith('/topup')).length,1);await pay.click();
  await page.waitForTimeout(100);const topups=requests.filter(x=>x.path.endsWith('/topup'));assert.equal(topups.length,2);
  assert(topups[0].body.intent_key);assert.equal(topups[0].body.intent_key,topups[1].body.intent_key);
 }],
 ['Persian RTL read failure is localized and recoverable',async({page,state,go,assert})=>{
  state.profile.locale='fa';let n=0;state.history=async()=>++n===1?{status:503,body:{error:'gateway_error'}}:{items:[]};await go();
  await page.getByRole('region',{name:'تراکنش‌ها',exact:true}).getByRole('button',{name:'تلاش دوباره',exact:true}).click();
  await page.getByText('هنوز تراکنشی نداری.',{exact:true}).waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');
 }],
 ['denied history is not rendered as an empty success',async({page,state,go,assert})=>{
  state.history=async()=>({status:403,body:{error:'forbidden'}});await go();await retry(page).waitFor();
  assert.equal(await region(page).getByText('No transactions yet.',{exact:true}).count(),0);
 }],
]).catch(e=>{console.error(e.message);process.exitCode=1});
