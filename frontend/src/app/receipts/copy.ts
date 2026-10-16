const copy = {
  title: ["Card receipts", "رسیدهای کارت‌به‌کارت"],
  loading: [
    "Loading verified payment state…",
    "در حال دریافت وضعیت ثبت‌شدهٔ پرداخت…",
  ],
  refresh: ["Refresh from server", "دریافت مجدد از سرور"],
  error: [
    "State could not be verified. Upload is blocked; this does not mean your receipt was rejected.",
    "وضعیت تأیید نشد؛ ارسال مسدود است. این به معنی رد رسید نیست.",
  ],
  uncertain: [
    "Upload result is unknown. Do not send again blindly. Refresh before any new manual attempt.",
    "نتیجهٔ ارسال نامشخص است؛ کورکورانه دوباره نفرست. پیش از تلاش دستی تازه، وضعیت را دوباره دریافت کن.",
  ],
  ack: [
    "I reviewed the payment and operator note. This image is sent to my Telegram chat and staff for review; it is not a payment confirmation.",
    "پرداخت و یادداشت اپراتور را بررسی کردم. تصویر به گفت‌وگوی تلگرام من و بررسی کارکنان می‌رسد؛ ارسال آن تأیید پرداخت نیست.",
  ],
  warning: [
    "Read the operator's instructions. Do not make another transfer solely because a correction was requested; contact support if the amount or destination is unclear.",
    "دستور اپراتور را بخوان. صرف درخواست اصلاح، دوباره پول واریز نکن؛ اگر مبلغ یا مقصد روشن نیست از پشتیبانی بپرس.",
  ],
  amountFix: ["Correction requested: amount", "درخواست اصلاح مبلغ رسید"],
  cardFix: [
    "Correction requested: destination card",
    "درخواست اصلاح کارت مقصد",
  ],
  note: ["Operator note", "یادداشت اپراتور"],
  noNote: [
    "No extra note. Ask support if instructions are unclear.",
    "یادداشت اضافی ندارد؛ اگر دستور روشن نیست از پشتیبانی بپرس.",
  ],
  amount: ["Recorded payment amount", "مبلغ ثبت‌شدهٔ پرداخت"],
  deadline: ["Receipt deadline", "مهلت ارسال رسید"],
  awaiting_receipt: ["Awaiting receipt", "منتظر رسید"],
  correction_required: ["Corrected receipt required", "نیازمند رسید اصلاح‌شده"],
  under_review: [
    "Under review — no further upload",
    "در حال بررسی؛ ارسال تازه ممکن نیست",
  ],
  paid: ["Payment recorded as paid", "پرداخت با وضعیت پرداخت‌شده ثبت شده"],
  closed: [
    "Payment closed; contact support",
    "پرداخت بسته شده؛ با پشتیبانی تماس بگیر",
  ],
  deadline_passed: ["Receipt deadline passed", "مهلت ارسال رسید گذشته است"],
  unknown: [
    "State unclear; contact support",
    "وضعیت نامشخص؛ با پشتیبانی تماس بگیر",
  ],
  not_card: ["Not a card payment", "این پرداخت کارت‌به‌کارت نیست"],
  initial: [
    "Use the destination supplied at checkout, then attach the receipt. Uploading does not transfer money.",
    "از مقصد اعلام‌شده در تسویه استفاده کن و سپس رسید را بفرست. ارسال رسید پولی منتقل نمی‌کند.",
  ],
  missingAccounts: [
    "Checkout card details are not available here. Ask support for the correct destination; do not guess or transfer again.",
    "اطلاعات کارت تسویه اینجا موجود نیست؛ مقصد صحیح را از پشتیبانی بپرس. حدس نزن و دوباره واریز نکن.",
  ],
  choose: [
    "Choose JPEG, PNG or WebP (up to 8 MiB)",
    "انتخاب JPEG، PNG یا WebP (حداکثر ۸ MiB)",
  ],
  invalid: [
    "Use a nonempty JPEG, PNG or WebP image up to 8 MiB.",
    "تصویر غیرخالی JPEG، PNG یا WebP تا ۸ MiB انتخاب کن.",
  ],
  preview: ["Selected receipt preview", "پیش‌نمایش رسید انتخاب‌شده"],
  send: ["Confirm and send receipt", "تأیید و ارسال رسید"],
  saved: [
    "The server recorded this upload. The status below is not proof of service delivery.",
    "سرور ارسال را ثبت کرد؛ وضعیت زیر به معنی تحویل سرویس نیست.",
  ],
  empty: [
    "No card payments on this page.",
    "پرداخت کارت‌به‌کارت در این صفحه نیست.",
  ],
  more: ["Older receipts", "رسیدهای قدیمی‌تر"],
  hint: [
    "Reopen a payment to read corrections and send a corrected receipt. No new payment is created here.",
    "پرداخت را باز کن تا درخواست اصلاح را ببینی و رسید اصلاح‌شده بفرستی. اینجا پرداخت تازه ساخته نمی‌شود.",
  ],
} as const;
export type CopyKey = keyof typeof copy;
export function translator(locale: string) {
  return (key: CopyKey): string => copy[key][locale.startsWith("fa") ? 1 : 0];
}
