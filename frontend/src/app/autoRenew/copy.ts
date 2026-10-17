const copy = {
  title: ["Automatic renewal / add-ons", "تمدید و افزونهٔ خودکار"],
  hint: [
    "Charged from your wallet balance at current prices.",
    "با موجودی کیف پول و قیمت روز انجام می‌شود.",
  ],
  loading: ["Loading…", "در حال دریافت…"],
  stopped: ["Stopped after an error", "پس از خطا متوقف شد"],
  waiting: ["On; waiting for wallet funds", "روشن؛ منتظر شارژ کیف پول"],
  unknown: ["Status unknown; check again", "وضعیت نامشخص؛ دوباره بررسی کن"],
  loadError: [
    "Settings could not be loaded; the status is unknown.",
    "تنظیمات دریافت نشد؛ وضعیت نامشخص است.",
  ],
  retry: ["Check again", "بررسی دوباره"],
  ineligible: [
    "This service cannot start automatic purchases right now.",
    "این سرویس اکنون امکان خرید خودکار ندارد.",
  ],
  onRenew: ["On; renewing with «{plan}»", "روشن؛ تمدید خودکار با پلن «{plan}»"],
  onExtra: ["On; automatic add-ons: {parts}", "روشن؛ افزونهٔ خودکار: {parts}"],
  gbUnit: ["GB", "گیگابایت"],
  dayUnit: ["days", "روز"],
  renew: ["Renew with a plan", "تمدید با پلن"],
  extra: ["Automatic add-ons", "افزونهٔ خودکار"],
  plan: ["Renewal plan", "پلن تمدید"],
  gb: ["Extra GB when data is low", "گیگابایت افزوده هنگام کمبود حجم"],
  days: ["Extra days near expiry", "روز افزوده نزدیک انقضا"],
  cancel: ["Cancel", "انصراف"],
  save: ["Save setting", "ذخیرهٔ تنظیم"],
  saved: ["Setting saved", "تنظیم ذخیره شد"],
  enabled: ["Automatic renewal turned on", "تمدید خودکار روشن شد"],
  disabled: ["Automatic renewal turned off", "تمدید خودکار خاموش شد"],
  invalid: [
    "Pick valid available options; choose at least one add-on.",
    "گزینه‌های موجود و معتبر را انتخاب کن؛ حداقل یک افزونه لازم است.",
  ],
  uncertain: [
    "The save result could not be verified; check again.",
    "نتیجهٔ ذخیره تأیید نشد؛ دوباره بررسی کن.",
  ],
  changed: [
    "The setting changed elsewhere; check again.",
    "تنظیم جای دیگری تغییر کرده؛ دوباره بررسی کن.",
  ],
  rejected: [
    "The server rejected this change; check again.",
    "سرور این تغییر را نپذیرفت؛ دوباره بررسی کن.",
  ],
} as const;
export type Key = keyof typeof copy;
export const translator = (locale: string) => (key: Key) =>
  copy[key][locale === "fa" ? 1 : 0];
