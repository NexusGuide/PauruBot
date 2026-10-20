const {run}=require('./screen-browser.cjs');
const open=async p=>{await p.getByRole('button',{name:'Rename service',exact:true}).click();return p.getByRole('textbox',{name:'Service display name',exact:true})};
const save=p=>p.getByRole('button',{name:'Save name',exact:true});
run('rename',[
 ['open, autofocus and cancel never send rename',async({page,requests,go,assert})=>{
  await go();const input=await open(page);assert.equal(await input.inputValue(),'Original');await input.fill('Draft');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();assert.equal(requests.filter(x=>x.path.endsWith('/rename')).length,0);
  await page.getByRole('heading',{name:'Original',exact:true}).waitFor();
 }],
 ['normalized rename uses existing API and displays server confirmation',async({page,state,requests,go,assert,service})=>{
  state.rename=async(id,body)=>({service:service(id,body.label+' confirmed')});await go();await(await open(page)).fill('  New name  ');await save(page).click();
  await page.getByRole('heading',{name:'New name confirmed',exact:true}).waitFor();const [req]=requests.filter(x=>x.path.endsWith('/rename'));
  assert.deepEqual(req.body,{label:'New name'});assert.equal(req.method,'POST');assert.equal(req.path,'/api/webapp/services/7/rename');await page.getByText('fixed_7',{exact:true}).waitFor();
 }],
 ['blank and unchanged values cannot submit, maxlength bounds typing',async({page,requests,go,assert})=>{
  await go();const input=await open(page);assert.equal(await save(page).isDisabled(),true);await input.fill('   ');assert.equal(await save(page).isDisabled(),true);
  await input.fill('x'.repeat(60));assert.equal((await input.inputValue()).length,48);assert.equal(requests.filter(x=>x.path.endsWith('/rename')).length,0);
 }],
 ['rapid submit sends once; config and revoke controls remain exclusive',async({page,state,requests,go,defer,assert,service})=>{
  const pending=defer();state.rename=()=>pending.promise;await go();await(await open(page)).fill('Draft');
  await page.getByRole('form',{name:'Rename service',exact:true}).evaluate(f=>{f.requestSubmit();f.requestSubmit()});
  await page.waitForFunction(()=>document.querySelector('input#service-label')?.disabled);
  assert.equal(requests.filter(x=>x.path.endsWith('/rename')).length,1);assert.equal(await page.getByRole('button',{name:'Show configs',exact:true}).isDisabled(),true);
  assert.equal(await page.getByRole('button',{name:'New link',exact:true}).isDisabled(),true);assert.equal(await page.getByRole('button',{name:'Cancel',exact:true}).isDisabled(),true);
  pending.resolve({service:service(7,'Saved')});await page.getByRole('heading',{name:'Saved',exact:true}).waitFor();
 }],
 ['failed rename preserves input; no automatic mutation retry',async({page,state,requests,go,assert,service,sleep})=>{
  let n=0;state.rename=async()=>++n===1?{status:503,body:{error:'panel_error'}}:{service:service(7,'Saved')};await go();const input=await open(page);await input.fill('Draft');await save(page).click();
  await page.getByRole('form',{name:'Rename service',exact:true}).getByRole('alert').waitFor();assert.equal(await input.inputValue(),'Draft');
  await sleep(100);assert.equal(requests.filter(x=>x.path.endsWith('/rename')).length,1);await page.getByRole('heading',{name:'Original',exact:true}).waitFor();
  await save(page).click();await page.getByRole('heading',{name:'Saved',exact:true}).waitFor();assert.equal(n,2);
 }],
 ['late success from old route cannot rename another service or toast',async({page,state,go,defer,assert,service,sleep})=>{
  const pending=defer();state.rename=()=>pending.promise;await go();await(await open(page)).fill('Draft');await save(page).click();await page.waitForFunction(()=>document.querySelector('#service-label')?.disabled);
  await page.evaluate(()=>window.qaGo('/services/8'));await page.getByText('fixed_8',{exact:true}).waitFor();pending.resolve({service:service(7,'Late')});await sleep(100);
  await page.getByRole('heading',{name:'Original',exact:true}).waitFor();assert.equal(await page.getByText('Service name saved.',{exact:true}).count(),0);assert.deepEqual(await page.evaluate(()=>window.feedback||[]),[]);
 }],
 ['late failure after leaving does not surface in new route',async({page,state,go,defer,assert,sleep})=>{
  const pending=defer();state.rename=()=>pending.promise;await go();await(await open(page)).fill('Draft');await save(page).click();await page.waitForFunction(()=>document.querySelector('#service-label')?.disabled);
  await page.evaluate(()=>window.qaGo('/other'));pending.resolve({status:503,body:{error:'panel_error'}});await sleep(100);assert.equal(await page.getByRole('alert').count(),0);
 }],
 ['Persian RTL supports Unicode display names without changing username',async({page,state,go,assert})=>{
  state.profile.locale='fa';await go();await page.getByRole('button',{name:'تغییر نام سرویس',exact:true}).click();
  const input=page.getByRole('textbox',{name:'نام نمایشی سرویس',exact:true});await input.fill('  مشتری اول  ');await page.getByRole('button',{name:'ذخیرهٔ نام',exact:true}).click();
  await page.getByRole('heading',{name:'مشتری اول',exact:true}).waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');await page.getByText('fixed_7',{exact:true}).waitFor();
 }],
 ['HTML-looking labels are escaped as text',async({page,go,assert})=>{
  await go();const text='<img src=x onerror=alert(1)>';await(await open(page)).fill(text);await save(page).click();await page.getByRole('heading',{name:text,exact:true}).waitFor();assert.equal(await page.locator('h1 img').count(),0);
 }],
 ['denied rename retains draft and never claims success',async({page,state,go,assert})=>{
  state.rename=async()=>({status:404,body:{error:'service_not_found'}});await go();const input=await open(page);await input.fill('Draft');await save(page).click();
  await page.getByRole('form',{name:'Rename service',exact:true}).getByRole('alert').waitFor();assert.equal(await input.inputValue(),'Draft');assert.equal(await page.getByText('Service name saved.',{exact:true}).count(),0);
 }],
]).catch(e=>{console.error(e.message);process.exitCode=1});
