const GB = 1024 ** 3;

export function money(amount: number, locale: string): string {
  return new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US").format(amount);
}

export function size(bytes: number, locale: string, units: string[]): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit >= 3 ? 2 : 0;
  const text = new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US", {
    maximumFractionDigits: digits,
  }).format(value);
  return `${text} ${units[unit]}`;
}

export function gb(bytes: number): number {
  return bytes / GB;
}

export function date(iso: string | null, locale: string): string {
  if (!iso) return "—";
  const options: Intl.DateTimeFormatOptions = { year: "numeric", month: "short", day: "numeric" };
  return new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", options).format(new Date(iso));
}

export function dateTime(iso: string | null, locale: string): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat(locale === "fa" ? "fa-IR-u-ca-persian" : "en-GB", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

export function daysLeft(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return ms <= 0 ? 0 : Math.ceil(ms / 86_400_000);
}

export function digits(value: number | string, locale: string): string {
  return new Intl.NumberFormat(locale === "fa" ? "fa-IR" : "en-US", { useGrouping: false }).format(
    Number(value),
  );
}

// Mirror of paurubot/core/formatting.py Jalali names (transliterated).
export const JALALI_MONTHS = [
  "Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
  "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand",
];

// Monday-first, matching (getDay() + 6) % 7.
const JALALI_WEEKDAYS = [
  "Doshanbeh", "Seshanbeh", "Chaharshanbeh", "Panjshanbeh", "Jomeh", "Shanbeh", "Yekshanbeh",
];

export function gregorianToJalali(gy: number, gm: number, gd: number): [number, number, number] {
  const gDays = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
  const gy2 = gm > 2 ? gy + 1 : gy;
  const gDay = gDays[gm - 1] ?? 0;
  let days =
    355666 + 365 * gy + Math.floor((gy2 + 3) / 4) - Math.floor((gy2 + 99) / 100) +
    Math.floor((gy2 + 399) / 400) + gd + gDay;
  let jy = -1595 + 33 * Math.floor(days / 12053);
  days %= 12053;
  jy += 4 * Math.floor(days / 1461);
  days %= 1461;
  if (days > 365) {
    jy += Math.floor((days - 1) / 365);
    days = (days - 1) % 365;
  }
  let jm: number;
  let jd: number;
  if (days < 186) {
    jm = 1 + Math.floor(days / 31);
    jd = 1 + (days % 31);
  } else {
    jm = 7 + Math.floor((days - 186) / 30);
    jd = 1 + ((days - 186) % 30);
  }
  return [jy, jm, jd];
}

export function jalaliMonthName(month: number): string {
  if (!Number.isInteger(month) || month < 1 || month > 12) {
    throw new RangeError(`invalid Jalali month: ${month}`);
  }
  return JALALI_MONTHS[month - 1] ?? "";
}

export function jalaliWeekdayName(day: Date): string {
  return JALALI_WEEKDAYS[(day.getDay() + 6) % 7] ?? "";
}

const FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹";

export function persianDigits(value: string | number): string {
  return String(value).replace(/[0-9]/g, (d) => FA_DIGITS.charAt(Number(d)));
}

export function jstrftimeFa(iso: string | null, withTime = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const [jy, jm, jd] = gregorianToJalali(d.getFullYear(), d.getMonth() + 1, d.getDate());
  let text = `${jalaliWeekdayName(d)} ${jd} ${jalaliMonthName(jm)} ${jy}`;
  if (withTime) {
    text += ` ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return persianDigits(text);
}
