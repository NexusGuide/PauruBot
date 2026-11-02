# بازبینی ربات‌های مرجع و تطبیق ممیزی v1/v2 با کد فعلی — نوامبر ۲۰۲۶

## مبنا

| پروژه | snapshot ممیزی | نسخهٔ بررسی‌شده در این سند | تغییر از ممیزی تا امروز |
|---|---|---|---|
| PauruBot | `d1a4c1d` (۲۰۲۶-۱۰-۱۷) | `554491c` (#129) | ۲۸۶ commit، ۲۹۹ فایل، ‎+۲۸٬۴۵۴ خط |
| A — mirzabot | `1ac06bb` | `6c0944b` (۲۰۲۶-۱۰-۲۴) | ۱۰ commit، ۷۳ فایل |
| B — Faoxima | `a96ccf2` | `e17c188` (۲۰۲۶-۱۰-۲۱) | ۲ commit، فقط `install.sh` |
| C — PGClockBot | `f5f50cf` | `d769096` (۲۰۲۶-۱۰-۱۹) | **۶۰۸ commit، ۷۲۹ فایل، ‎+۶۴٬۵۵۶ خط** |
| D — PasarguardBot | `1830399` | `64cb1c4` (۲۰۲۶-۱۰-۱۷) | ۴۸ commit، ۵۳۹ فایل |

نتیجهٔ کلی: **ممیزی v2 دیگر تصویر دقیقی از PauruBot نیست.** بیشتر یافته‌های امنیتی و شکاف‌های Mini App آن بعد از snapshot بسته شده‌اند. در مقابل، PGClockBot و PasarguardBot بعد از ممیزی قابلیت‌های تازه‌ای گرفته‌اند که در ممیزی نیامده‌اند.

روش کار: بررسی ایستای کد (grep و خواندن مسیرهای مربوط). هیچ تستی اجرا نشد، هیچ سرویس واقعی‌ای راه‌اندازی نشد و هیچ تغییری در کد داده نشد.

---

## ۱. وضعیت یافته‌های امنیتی ممیزی v2 (S01–S09) در کد فعلی

| یافته | وضعیت فعلی | شاهد |
|---|---|---|
| S01 ارسال خطای یک shop به همهٔ tenantها | ✅ رفع شد | `logs.py` حالا `shop_id` را در envelope می‌گذارد. `notifications/error_reports.py` خطا را فقط به کانال errors همان shop می‌فرستد، یا اگر shop نامعلوم باشد فقط به `owner_telegram_ids`. `deliver()` هم مقصد را دوباره بررسی می‌کند. |
| S02 بریدن رمز در ۷۲ بایت | ✅ رفع شد (با باقیماندهٔ legacy) | `security/tokens.py`: hashهای جدید Argon2id هستند. hashهای قدیمی bcrypt همچنان با `[:72]` verify می‌شوند و عمداً rehash خودکار ندارند. این رفتار مستند است، ولی کاربران قدیمی تا تعویض رمز همان collision را دارند. |
| S03 اعتماد کارت قبل از سقف/ریسک | ✅ رفع شد | `payments/engine.py:auto_approvable`: اول flag، بعد سقف (سقف ≤۰ یعنی رد)، بعد trust. trust فقط شرط سابقه را کنار می‌گذارد. |
| S04 overwrite وضعیت با webhook کهنه | ✅ رفع شد | webhook دیگر state را اعمال نمی‌کند. `apply_webhook_user` فقط job `refresh_service` می‌سازد و پاسخ `applied: 0` است. |
| S05 DNS rebinding / TOCTOU | ✅ رفع شد | `panels/transport.py` ← `security/http.ValidatedTransport`: اتصال به همان IPهای validateشده bind می‌شود. |
| S06 خواندن کامل body پیش از سنجش سقف | ✅ رفع شد | `api/body.read_limited_body` در webhook با `MAX_BODY=512KiB` و خطای 413. |
| S07 idempotency دفترکل بدون fingerprint | ✅ رفع شد | `ledger/posting.py` استفادهٔ دوباره از key برای journal متفاوت را با `LedgerError` رد می‌کند. |
| S08 race سهمیهٔ کلی trial | ✅ رفع شد | `core/trials.py`: یک `pg_advisory_xact_lock` روی هر shop. |
| S09 ساخت intent پرداخت تکراری | ✅ رفع شد | `TopupBody.intent_key` به `payments.intents.start_once` می‌رود. در تعارض، خطای 409 `payment_intent_conflict` برمی‌گردد. |

نکتهٔ کوچک تازه: webhook پنل برای هر رویداد یک `refresh_service` بدون dedupe می‌سازد (سقف `MAX_EVENTS` در هر درخواست). مهاجم بیرونی نمی‌تواند از آن سوءاستفاده کند، چون secret لازم است. ولی پنلی که پشت‌سرهم webhook بفرستد صف provision را پر می‌کند. یک dedupe کوتاه‌مدت مثل `refresh:{service_id}` ارزان است.

## ۲. وضعیت backlog ممیزی (F-01…F-34)

**بسته‌شده از زمان ممیزی:**

| آیتم | شاهد در کد فعلی |
|---|---|
| F-01 جست‌وجو و صفحه‌بندی خدمات | `GET /api/webapp/services?q=&status=&limit=&before=` و فیلد جست‌وجو در `Services.tsx` |
| F-02 پیوست تیکت در Mini App | `webapp_ticket_media.py` برای upload و download، به‌همراه job پاک‌سازی `ticket_media.cleanup` |
| F-07 خطای قابل retry در جزئیات سرویس | `ServiceDetail` حالا `ReadFailure` با retry نشان می‌دهد و redirect نمی‌کند |
| F-08 فیلتر تیکت روی کل تاریخچه | فیلتر سمت سرور `status` و `department_id` با cursor |
| F-10 پیش‌نمایش تمدید قبل/بعد | `GET /orders/{id}/renewal-preview` و `core/renewal_preview.py` |
| F-16 انتقال کیف‌پول در Mini App | `webapp_transfers.py` شامل policy، quote و confirm، و `WalletTransfer.tsx` |
| F-17 احراز کارت در Mini App | `webapp_cards.py` و `CardVerification.tsx` |
| F-18 صندوق اطلاعیهٔ سروری با seen | `announcements.py` و `AnnouncementInbox.tsx` |
| F-23/F-24/F-25/F-26 | همان S05/S06، S08، S09 و S04 |
| F-27 خطای کیف‌پول | `WalletHistory` با `useResource` و `ReadFailure` |
| F-29 ادامهٔ پرداخت نیمه‌تمام | `webapp_payment_inbox.py`: `GET /payments` و `/payments/{id}/resume` |
| F-30 تیکت با زمینهٔ سرویس | `TicketBody.service_id` |

**هنوز باز (به ترتیب ارزش پیشنهادی):**

| آیتم | وضعیت | پیشنهاد |
|---|---|---|
| F-03 parity عملیات سرویس | Mini App فقط `rename` و `revoke` دارد. خاموش/روشن، لغو و جابجایی لوکیشن ندارد. | P2. با capability map سرور و preview/confirm. |
| F-19 گروه‌بندی خدمات بر اساس پنل | ندارد | P2 برای نماینده‌های پرسرویس |
| F-11 metadata دسترسی کلاینت (IP/app) | driverها `client_ips` دارند، ولی UI در Mini App نیست | P2، با masking |
| F-12 contact امضاشده داخل Mini App | فقط Bot. Mini App تلفن را masked نشان می‌دهد. | P2 |
| F-21 manifest و PWA مشتری | `frontend/public/app` آیکن دارد ولی `manifest.webmanifest` ندارد | P3 |
| F-34 brand accent/logo در `/me` | ندارد | P3. با «تم Mini App» بخش ۳ ادغام شود. |
| F-32 انتخاب فرمت کلاینت | ندارد (فقط UA passthrough) | P3 |
| F-33 راهنمای تخفیف بر پایهٔ eligibility | ندارد | P3 |
| F-31 یادداشت شخصی سرویس | ندارد | P3 / فعلاً نه |
| F-22 reaction تیکت | ندارد | P3 |
| F-14 امتیاز و فروشگاه جایزه | ندارد (tier خرج، lottery و gift code هست) | P3. اول مدل اقتصادی و ضدسوءاستفاده تعریف شود. |
| F-20 سهمیهٔ trial به تفکیک پنل | فقط سهمیهٔ shop | P3 |

## ۳. قابلیت‌های تازهٔ مرجع‌ها (بعد از ممیزی) که در ممیزی نیستند

### C — PGClockBot (۸۹ commit از نوع feat)
| قابلیت | در PauruBot | ارزیابی |
|---|---|---|
| **پایش سلامت اتصال با Xray**: نصب خودکار هستهٔ تست، batch ایزوله، تست HTTPS پراکسی‌شده، snapshot وضعیت برای مشتری و دکمهٔ «وضعیت اتصال» | ❌ (فقط uptime و آمار پنل در `monitor_panels`) | **ارزشمند، P2.** «پنل بالا است» با «کانفیگ واقعاً وصل می‌شود» فرق دارد. باید در worker جدا و با سقف منابع اجرا شود. |
| افزودن سرویسی که بیرون از ربات ساخته شده توسط مشتری، با تأیید ادمین | ⚠️ ابزار migration ادمین هست، self-claim مشتری نیست | P2. با تأیید ادمین و اثبات مالکیت (لینک subscription). باید با SEC-F03 (رزرو panel+username) هماهنگ شود. |
| پاک‌سازی چت: فقط جدیدترین پیام home بماند و پیام‌های قدیمی منو حذف شوند | ❌ | P3 UX. کم‌هزینه است. |
| ویرایش متن و رنگ هر دکمه از داخل منو و پنل وب، جست‌وجوی تنظیمات با تحمل غلط تایپی | ⚠️ `BotText` و `BotMenu.tsx` هست | P3. جست‌وجوی تنظیمات مفید است. |
| تخفیف scoped و کاهش دائمی قیمت | ⚠️ `Discount.plan_ids` هست | تقریباً معادل است. «کاهش دائمی» با تغییر قیمت plan هم ممکن است. |
| هشدار پلکانی موجودی کم برای نمایندهٔ PAYG | ⚠️ `billing_mode` و `credit_limit` هست | بررسی شود که هشدار پلکانی به نماینده و مالک وجود دارد یا نه. |
| ارسال backup به تلگرام با retention | ✅ `ops/delivery.send_backup` | معادل |
| کارت تصادفی در کارت‌به‌کارت | ✅ `core/cards.py` (`secrets.choice`) | معادل |
| درگاه BezanBeCard | ❌ | بر اساس تقاضای بازار |

### D — PasarguardBot (v3.1.0)
| قابلیت | در PauruBot | ارزیابی |
|---|---|---|
| **تم رنگی Mini App توسط ادمین و theme pack اشتراکی** (متن و دکمه) | ❌ | P3. همراه F-34 در یک کار «برند Mini App». |
| مدیریت دکمه‌های منوی help از پنل وب | ✅ `help_items` در settings | معادل |
| کارت آمار کاربران جدید ۲۴ ساعت اخیر | ⚠️ در `admin_hub` بات هست | در Home پنل وب هم نمایش داده شود (کم‌هزینه). |
| trial نامحدود برای ادمین اصلی | ❌ | ضروری نیست |
| اصلاحات کارایی و امنیت (مچ tx رمزارز قبل از expire، یک tx برای یک invoice، mask خطای داخلی، polling فقط در تب فعال) | — | چک‌لیست مفید برای بازبینی خودمان، به‌خصوص با SEC-F18 (ابهام رمزارز). |

### A — mirzabot (v0.8 و v0.9)
نمودار فروش و پرداخت در داشبورد (✅ داریم: `admin/chart.tsx`)، ترتیب نمایش محصول (✅)، donation (❌، ضروری نیست)، درگاه cubepay و اعتبارسنجی ورودی. **شکاف مهمی ایجاد نکرده است.**

### B — Faoxima
فقط `install.sh` تغییر کرده است. نتایج ممیزی v2 برای B هنوز معتبر است.

## ۴. اولویت واقعی پیشنهادی

مهم‌ترین کار مقایسه با مرجع‌ها نیست. مهم‌ترین کار **یافته‌های باز امنیتی خود پروژه** است که در `docs/security/audit-remediation-2026-10.md` آمده‌اند. آن سند صریحاً F01، F02، F08، F09 و F10 را پیش‌شرط انتشار می‌داند:

1. **P0/P1، یافته‌های باز SEC:** F01 (اعتبار ارتقا)، F02 (تخفیف در تسویه)، F08 (نتیجهٔ نامعلوم ساخت remote)، F09 (قفل sync/cleanup/modify)، F10 (پوشش اسکن زنجیره)، F03 (رزرو پایدار username)، F11 (گیت تلفن و قوانین در خرید Mini App)، F15، F16، F22، F24، F26 و F27.
2. **P2، parity و ارزش محصول:** F-03 (عملیات سرویس در Mini App)، پایش سلامت اتصال (C)، self-claim سرویس بیرونی با تأیید ادمین (C)، F-19، F-11 و F-12.
3. **P3، برند و جزئیات:** تم و brand در Mini App (D، F-34 و F-21)، پاک‌سازی چت، F-31 تا F-33، reaction و امتیاز.

### چیزهایی که نباید کپی شوند (همچنان معتبر از ممیزی v2)
- الگوریتم انتقال کیف‌پول B (debit و credit جدا با compensation)، پیام موفقیت بدون تحویل در `report_problem` و `update_info` نمایشی B.
- نگه‌داشتن intent پرداخت فقط در Redis (D)، guard تک‌ادمینی `ADMIN_ID` (D)، و `MockPspAdapter` (C) در production.
