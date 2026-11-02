/* REF-03 browser QA: npm run build first; requires Playwright + a local Chromium.
 * The Mini App starts and signs in when telegram.org hangs or fails, using the launch data
 * Telegram put in the address, and explains the problem when there is none.
 * CHROMIUM_EXECUTABLE=$(command -v chromium) node tests/browser/launch-qa.cjs
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '../../dist');
const profile = {id:1,telegram_id:1,name:'Synthetic QA',username:'synthetic',locale:'en',balance:0,currency:'IRT',shop:{title:'Synthetic shop',bot_username:'test_bot',support:null,languages:['en','fa']},trial:{available:false,reason:null,days:1,gb:1},referral:{enabled:false,percent:0,count:0},reseller:{active:false,discount_percent:0},panel_accounts:false,extras:{enabled:false,upgrade:false},payment_methods:['wallet'],topup_bounds:[1,100000],staff:null,preferences:{marketing_opt_out:false,changed_at:null},alerts_muted:false,profile:{phone_masked:null,member_since:'2026-01-01'}};
const SIGNED = 'query_id=AAE&user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc';
const FAKE_SDK = 'window.Telegram={WebApp:{initData:"from-sdk",initDataUnsafe:{},colorScheme:"light",ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){},offClick(){}},MainButton:{}}};';

async function run() {
  const server = http.createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    let file = path.resolve(root, '.' + pathname);
    if (!file.startsWith(root + path.sep)) { res.writeHead(403); return res.end(); }
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(root, 'app/index.html');
    res.setHeader('content-type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream');
    fs.createReadStream(file).pipe(res);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE || undefined, args: ['--no-sandbox'] });
  async function open(telegram, hash) {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const seen = [];
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.hostname === 'telegram.org') return telegram(route);
      if (url.hostname !== '127.0.0.1') return route.abort();
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const data = route.request().headers()['x-telegram-init-data'] || '';
      seen.push([url.pathname, data]);
      if (!data) return route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: 'invalid_init_data' }) });
      if (url.pathname === '/api/webapp/me') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(profile) });
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], channels: [] }) });
    });
    const started = Date.now();
    await page.goto(`${base}/app/${hash}`, { waitUntil: 'domcontentloaded' });
    return { page, seen, errors, started };
  }
  const hanging = () => new Promise(() => {});
  try {
    // telegram.org never answers: the app goes on after a short wait and signs in from the address.
    let { page, seen, errors, started } = await open(hanging, `#tgWebAppData=${encodeURIComponent(SIGNED)}&tgWebAppVersion=7.0`);
    await page.getByText('Synthetic shop').first().waitFor({ timeout: 8000 });
    assert.ok(Date.now() - started < 7000, 'the app waited too long for telegram.org');
    assert.deepEqual(seen.find(([p]) => p === '/api/webapp/me'), ['/api/webapp/me', SIGNED]);
    assert.deepEqual(errors, []);
    await page.close();

    // telegram.org fails and there is no launch data: say what is wrong (the default language is Persian).
    ({ page, errors } = await open((route) => route.abort(), ''));
    await page.getByText('اتصال به تلگرام برقرار نشد').waitFor({ timeout: 8000 });
    assert.deepEqual(errors, []);
    await page.close();

    // The SDK loads: its own copy of the launch data is used.
    ({ page, seen } = await open((route) => route.fulfill({ status: 200, contentType: 'application/javascript', body: FAKE_SDK }), ''));
    await page.getByText('Synthetic shop').first().waitFor({ timeout: 8000 });
    assert.deepEqual(seen.find(([p]) => p === '/api/webapp/me'), ['/api/webapp/me', 'from-sdk']);
    await page.close();
    console.log('launch QA: 3 scenarios passed');
  } finally {
    await browser.close();
    server.close();
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
