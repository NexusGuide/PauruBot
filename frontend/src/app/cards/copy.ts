const en = {
  title: "Card verification", intro: "Register your card for staff review. This is not a payment receipt and does not start or confirm a payment.",
  number: "Card number", photo: "Card photograph (JPEG/PNG, up to 3 MiB)",
  send: "Send for review", retry: "Retry the same submission", refresh: "Refresh status",
  pending: "Waiting for staff review", approved: "Approved", rejected: "Rejected",
  disabled: "Card verification is not enabled for this shop.", blocked: "Card payments are blocked. Contact support.",
  trusted: "This shop has exempted your account from card verification.",
  minimum: "Verification threshold", invalid: "Enter a valid 16-digit card number and a JPEG/PNG photograph up to 3 MiB.",
  failed: "The result could not be confirmed. Refresh status or retry the same submission. Do not submit a different card yet.",
  load_failed: "Could not load verification status. Refresh to try again.",
  privacy: "The photograph is sent through the shop bot for staff review. Card numbers and photos are not saved in browser storage. Hide unrelated sensitive information in the photo.",
  back: "Back to wallet", sent: "Sent for review. Approval is not automatic; return to checkout and deliberately retry payment after approval.",
};
const fa: typeof en = {
  title: "تأیید کارت", intro: "کارت خود را برای بررسی کارکنان ثبت کنید. این بخش رسید پرداخت نیست و پرداختی ایجاد یا تأیید نمی‌کند.",
  number: "شماره کارت", photo: "عکس کارت (JPEG یا PNG، حداکثر ۳ مگابایت)",
  send: "ارسال برای بررسی", retry: "تلاش مجدد با همان درخواست", refresh: "به‌روزرسانی وضعیت",
  pending: "در انتظار بررسی کارکنان", approved: "تأیید شده", rejected: "رد شده",
  disabled: "تأیید کارت برای این فروشگاه فعال نیست.", blocked: "پرداخت کارت شما مسدود است؛ با پشتیبانی تماس بگیرید.",
  trusted: "فروشگاه حساب شما را از تأیید کارت معاف کرده است.", minimum: "حداقل مبلغ مشمول تأیید",
  invalid: "شماره کارت معتبر ۱۶ رقمی و عکس JPEG یا PNG با حجم حداکثر ۳ مگابایت وارد کنید.",
  failed: "نتیجه درخواست مشخص نیست. وضعیت را تازه کنید یا همان درخواست را دوباره بفرستید؛ فعلاً کارت دیگری ثبت نکنید.",
  load_failed: "دریافت وضعیت ناموفق بود؛ دوباره وضعیت را تازه کنید.",
  privacy: "عکس از طریق ربات فروشگاه برای بررسی کارکنان ارسال می‌شود. شماره و عکس کارت در حافظهٔ پایدار مرورگر ذخیره نمی‌شوند. اطلاعات حساس نامرتبط را در عکس بپوشانید.",
  back: "بازگشت به کیف پول", sent: "برای بررسی ارسال شد. تأیید خودکار نیست؛ پس از تأیید، به تسویه برگردید و پرداخت را خودتان دوباره انتخاب کنید.",
};
export const cardCopy = (locale: string) => locale.startsWith("fa") ? fa : en;
