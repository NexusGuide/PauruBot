# باز شدن Mini App بدون دسترسی به telegram.org (REF-03)

> ایده از PGClockBot (dev، نسخهٔ 0.5.26): «open the Mini App when telegram.org cannot be reached».

## مشکل

صفحهٔ Mini App اسکریپت SDK تلگرام (`https://telegram.org/js/telegram-web-app.js`) را به‌صورت blocking در `<head>` بار می‌کرد. کاربری که فقط از پروکسی داخلی تلگرام استفاده می‌کند در شبکهٔ خودِ webview به telegram.org دسترسی ندارد. نتیجه این بود که:

- صفحه تا پایان timeout این درخواست سفید می‌ماند؛
- بعد هم `initData` خالی بود و ورود با «از دکمهٔ ربات باز کنید» شکست می‌خورد.

## راه‌حل

- **`frontend/public/app/launch.js`** (فایل same-origin، چون CSP اسکریپت inline را منع می‌کند):
  - `initData` امضاشده را از نشانی صفحه (`#tgWebAppData=...`) می‌خواند. تلگرام همیشه آن را آنجا می‌گذارد.
  - پارامترها را همان‌جایی ذخیره می‌کند که خود SDK نگه می‌دارد (`sessionStorage.__telegram__initParams`). پس اگر SDK دیر برسد، پارامترها را پیدا می‌کند.
  - SDK را async بار می‌کند.
- **`telegram.ts`:**
  - `tg` یک live binding است و هر وقت SDK برسد پر می‌شود.
  - `initData()` نسخهٔ SDK را برمی‌گرداند و اگر نبود، نسخهٔ نشانی صفحه را.
  - برنامه حداکثر ۲٫۵ ثانیه منتظر SDK می‌ماند و بعد بدون آن ادامه می‌دهد.
  - اگر SDK دیرتر برسد، همچنان وصل می‌شود: `ready`، `expand`، تم و دکمه‌ها.
- **بدون SDK:** دکمه‌های native تلگرام و درخواست شماره نیستند، ولی ورود و همهٔ صفحه‌ها کار می‌کنند. لینک‌ها با `window.open` باز می‌شوند.
- **پیام خطا:** اگر نه `initData` هست و نه SDK، به‌جای «از ربات باز کنید» پیام «اتصال به تلگرام برقرار نشد» و راهنمای روشن کردن فیلترشکن نشان داده می‌شود.

امنیت تغییری نکرده است: سرور همان `initData` را با امضای HMAC و `auth_date` بررسی می‌کند (`webapp_auth`)، از هر جا که آمده باشد.

## آزمون

`frontend/tests/browser/launch-qa.cjs` با Chromium واقعی سه حالت را بررسی می‌کند:

1. telegram.org پاسخ نمی‌دهد: برنامه زیر ۷ ثانیه بالا می‌آید و با `initData` نشانی وارد می‌شود.
2. telegram.org خطا می‌دهد و `initData` نیست: پیام «اتصال به تلگرام برقرار نشد» نشان داده می‌شود.
3. SDK بار می‌شود: نسخهٔ SDK از `initData` استفاده می‌شود.

```bash
cd frontend && npm run build && node tests/browser/launch-qa.cjs
```
