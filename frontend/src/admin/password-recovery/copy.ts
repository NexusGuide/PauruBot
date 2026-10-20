export const copy = {
  title: ["Recover / rotate password", "بازیابی / تعویض امن رمز"],
  intro: ["Legacy passwords must use independent recovery. A 5-minute token is sent only to Telegram already linked by your administrator. No linked Telegram? Ask an authorized administrator; this form cannot change the destination.", "رمزهای قدیمی به بازیابی مستقل نیاز دارند. توکن ۵ دقیقه‌ای فقط به Telegram ازقبل‌متصل‌شده توسط مدیر فرستاده می‌شود. اگر Telegram متصل نیست، از مدیر مجاز کمک بگیر؛ این فرم مقصد را تغییر نمی‌دهد."],
  username: ["Username", "نام کاربری"],
  request: ["Request recovery token", "درخواست توکن بازیابی"],
  sent: ["If this is an active linked account, check its Telegram. Delivery is not confirmed by this message.", "اگر حساب فعال و متصل باشد، Telegram آن را بررسی کن. این پیام تأیید تحویل نیست."],
  token: ["Recovery token (not the 6-digit login code)", "توکن بازیابی (نه کد ۶ رقمی ورود)"],
  password: ["New password", "رمز جدید"],
  repeat: ["Repeat new password", "تکرار رمز جدید"],
  confirm: ["Change password and revoke sessions / API keys", "تغییر رمز و لغو نشست‌ها / کلیدهای API"],
  warning: ["Never share this token. Paste only here on your own admin site. All sessions and personal API keys will be revoked. You must sign in again.", "توکن را برای کسی نفرست؛ فقط در همین فرم پنل خودت وارد کن. تمام نشست‌ها و کلیدهای API شخصی لغو می‌شوند و باید دوباره وارد شوی."],
  done: ["Password reset. Sign in again using the new password. If no confirmation was received, do not assume success: try signing in or request a fresh token.", "رمز بازنشانی شد؛ با رمز جدید دوباره وارد شو. اگر تأیید دریافت نشد، موفقیت را فرض نکن: ورود را امتحان کن یا توکن تازه بگیر."],
  failed: ["Request failed or result unconfirmed. Token may be expired, used or invalid. Check connection and request a new one if needed.", "درخواست ناموفق یا نتیجه نامشخص است. ممکن است توکن منقضی، مصرف‌شده یا نامعتبر باشد. اتصال را بررسی کن و در صورت نیاز توکن تازه بگیر."],
  mismatch: ["Passwords must match and be 10–256 characters.", "رمزها باید یکسان و بین ۱۰ تا ۲۵۶ کاراکتر باشند."],
  close: ["Close and clear", "بستن و پاک‌کردن"],
} as const;
export const text = (locale: string, key: keyof typeof copy) => copy[key][locale === "fa" ? 1 : 0];
