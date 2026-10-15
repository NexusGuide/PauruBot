const copy = {
  dayUnit: ["days", "روز"],
  title: ["Automatic renewal / add-ons", "تمدید و افزونهٔ خودکار"],
  loading: ["Loading settings…", "در حال دریافت تنظیمات…"],
  unknown: ["Unknown", "نامشخص"],
  off: ["Off", "خاموش"],
  on: ["On", "روشن"],
  stopped: [
    "Stopped after an error; review before restarting",
    "متوقف پس از خطا؛ پیش از فعال‌سازی دوباره بررسی کن",
  ],
  waiting_balance: [
    "Waiting for wallet funds; remains enabled",
    "منتظر موجودی کیف پول؛ همچنان روشن است",
  ],
  reason: ["Stop reason", "علت توقف"],
  refresh: ["Refresh confirmed settings", "دریافت مجدد تنظیمات ثبت‌شده"],
  loadError: [
    "Settings could not be verified. This does not mean automatic renewal is off.",
    "تنظیمات تأیید نشد؛ این به معنی خاموش بودن تمدید خودکار نیست.",
  ],
  balance: [
    "Wallet balance at last successful read",
    "موجودی کیف پول در آخرین دریافت موفق",
  ],
  base: [
    "Current base price(s), before discounts; not a future invoice",
    "قیمت پایهٔ فعلی پیش از تخفیف؛ نه مبلغ قطعی سفارش آینده",
  ],
  rules: [
    "The worker checks roughly every minute. Renewal triggers near expiry (one hour) or low data (256 MiB or 2% of the limit, whichever is larger). Extra data/time are separate automatic orders. No charge is made just by saving; an eligible due service may be charged on the next worker pass.",
    "پردازشگر تقریباً هر دقیقه بررسی می‌کند؛ تمدید نزدیک پایان زمان (یک ساعت) یا حجم کم (۲۵۶ MiB یا ۲٪ سقف، هرکدام بیشتر باشد) انجام می‌شود. حجم و زمان افزوده سفارش‌های جدا دارند. ذخیرهٔ تنظیمات به‌خودی‌خود برداشت نمی‌کند؛ سرویس واجد شرایط ممکن است در بررسی بعدی پرداخت شود.",
  ],
  wallet: [
    "Automatic purchases use your wallet at the prices and eligibility in effect when each order is created. There is no new spending cap here. A short balance keeps the rule on and it may charge once funded; other failures can stop it. Already-paid/queued orders are not canceled by disabling this setting.",
    "خرید خودکار با کیف پول و قیمت و شرایط زمان ایجاد هر سفارش انجام می‌شود؛ این رابط سقف هزینهٔ جدیدی تعیین نمی‌کند. کمبود موجودی تنظیم را روشن نگه می‌دارد و پس از شارژ ممکن است برداشت شود؛ خطاهای دیگر می‌توانند آن را متوقف کنند. خاموش‌کردن، سفارش قبلاً پرداخت‌شده یا در صف را لغو نمی‌کند.",
  ],
  ineligible: [
    "New automatic purchases cannot be enabled for this service now. You can still turn an existing rule off.",
    "این سرویس اکنون امکان فعال‌سازی خرید خودکار ندارد؛ خاموش‌کردن تنظیم موجود همچنان ممکن است.",
  ],
  mode: ["Mode", "حالت"],
  renew: ["Renew with a plan", "تمدید با پلن"],
  extra: ["Automatic add-ons", "افزونهٔ خودکار"],
  plan: ["Renewal plan", "پلن تمدید"],
  choose: ["Choose an available plan", "پلن موجود را انتخاب کن"],
  gb: ["Extra GB when data is low", "گیگابایت افزوده هنگام کمبود حجم"],
  days: ["Extra days near expiry", "روز افزوده نزدیک انقضا"],
  none: ["None", "بدون افزونه"],
  review: ["Review change", "بررسی تغییر"],
  reviewTitle: [
    "Review automatic wallet purchases",
    "بررسی خرید خودکار از کیف پول",
  ],
  ack: [
    "I authorize this setting and understand automatic wallet charges and the limits above.",
    "این تنظیم را تأیید می‌کنم و برداشت خودکار از کیف پول و محدودیت‌های بالا را می‌پذیرم.",
  ],
  save: ["Confirm and save setting", "تأیید و ذخیرهٔ تنظیم"],
  edit: ["Back to editing", "بازگشت به ویرایش"],
  saved: [
    "Setting confirmed by the server. This is not proof of a renewal or payment.",
    "سرور ثبت تنظیم را تأیید کرد؛ این به معنی انجام تمدید یا پرداخت نیست.",
  ],
  invalid: [
    "Choose valid available options; use at least one add-on.",
    "گزینه‌های موجود و معتبر را انتخاب کن؛ حداقل یک افزونه لازم است.",
  ],
  uncertain: [
    "The save result could not be verified. Do not retry blindly. Refresh confirmed settings before a new manual change.",
    "نتیجهٔ ذخیره تأیید نشد؛ کورکورانه تکرار نکن. پیش از تغییر دستی تازه، تنظیمات ثبت‌شده را دوباره دریافت کن.",
  ],
  changed: [
    "The setting changed elsewhere. Refresh and review again; nothing was overwritten by this request.",
    "تنظیم در جای دیگری تغییر کرده؛ دوباره دریافت و بررسی کن. این درخواست چیزی را بازنویسی نکرد.",
  ],
  rejected: [
    "The server rejected this change. Refresh before reviewing a new attempt.",
    "سرور این تغییر را رد کرد؛ پیش از بررسی تلاش تازه، اطلاعات را دوباره دریافت کن.",
  ],
  current: [
    "Last confirmed rule (may be stale after an error)",
    "آخرین تنظیم تأییدشده (پس از خطا ممکن است قدیمی باشد)",
  ],
  missing: [
    "A previously selected option is no longer available; choose a new one or turn it off.",
    "گزینهٔ قبلی دیگر موجود نیست؛ گزینهٔ جدید انتخاب کن یا تنظیم را خاموش کن.",
  ],
  busy: ["Saving; do not submit again.", "در حال ذخیره؛ دوباره ارسال نکن."],
} as const;
export type Key = keyof typeof copy;
export const translator = (locale: string) => (key: Key) =>
  copy[key][locale === "fa" ? 1 : 0];
