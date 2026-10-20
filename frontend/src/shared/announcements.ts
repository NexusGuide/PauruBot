export interface Announcement {
  id: number;
  title: string;
  text: string;
  kind: "operational" | "marketing";
  starts_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  seen_at: string | null;
  read_at: string | null;
}
export interface AnnouncementPage {
  items: Announcement[];
  next_before: number | null;
}
export const announcementCopy = (locale: string) =>
  locale === "fa"
    ? {
        title: "صندوق اطلاعیه‌ها",
        alerts: "هشدارهای محاسبه‌شدهٔ سرویس و کیف پول",
        all: "همه",
        unread: "خوانده‌نشده",
        empty: "اطلاعیه‌ای نیست.",
        loading: "در حال دریافت…",
        retry: "تلاش دوباره",
        more: "بیشتر",
        refresh: "تازه‌سازی",
        open: "مشاهده",
        read: "خواندم",
        readDone: "خوانده‌شده",
        seen: "دیده‌شده",
        unseen: "دیده‌نشده",
        failure: "ثبت یا دریافت انجام نشد؛ دوباره امتحان کنید.",
        operational: "عملیاتی",
        marketing: "بازاریابی",
        publish: "انتشار در Mini App",
        revoke: "لغو انتشار",
        revoked: "لغوشده",
        heading: "عنوان",
        body: "متن ساده",
        start: "شروع (اختیاری)",
        end: "انقضا (اختیاری)",
        notice:
          "این صندوق جدا از ارسال تلگرام است. بازاریابی برای مشتریِ انصراف‌داده نمایش داده نمی‌شود. متن منتشرشده قابل ویرایش نیست؛ آن را لغو کنید و اطلاعیهٔ تازه بسازید.",
        confirm: "انتشار همین متن برای مشتریان فروشگاه؟",
        cancel: "انصراف",
        retryPublish: "تکرار همان انتشار",
        unknown: "نتیجه نامعلوم است؛ فقط همین انتشار را تکرار کنید.",
        next: "صفحهٔ بعد",
        first: "صفحهٔ اول",
      }
    : {
        title: "Announcement inbox",
        alerts: "Computed service and wallet alerts",
        all: "All",
        unread: "Unread",
        empty: "No announcements.",
        loading: "Loading…",
        retry: "Retry",
        more: "Load more",
        refresh: "Refresh",
        open: "Open",
        read: "Mark read",
        readDone: "Read",
        seen: "Seen",
        unseen: "Not seen",
        failure: "The read or update failed. Please retry.",
        operational: "Operational",
        marketing: "Marketing",
        publish: "Publish in Mini App",
        revoke: "Revoke publication",
        revoked: "Revoked",
        heading: "Title",
        body: "Plain text",
        start: "Starts (optional)",
        end: "Expires (optional)",
        notice:
          "Separate from Telegram delivery. Marketing is hidden for opted-out customers. Published text is immutable: revoke it and publish a new announcement.",
        confirm: "Publish this exact text to this shop’s customers?",
        cancel: "Cancel",
        retryPublish: "Retry same publication",
        unknown: "Result unknown. Only retry this same publication.",
        next: "Next page",
        first: "First page",
      };
