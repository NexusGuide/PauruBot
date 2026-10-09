from __future__ import annotations

import html
from datetime import UTC, datetime

from paurubot.config import get_settings

GB = 1024**3


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def money(amount: int | None) -> str:
    return f"{int(amount or 0):,}"


# Extended Arabic-Indic (U+06F0..) and Arabic-Indic (U+0660..) digits, built from code points.
_EXT_DIGITS = "".join(chr(0x06F0 + i) for i in range(10))
_ARABIC_DIGITS = "".join(chr(0x0660 + i) for i in range(10))
PERSIAN_DIGITS = str.maketrans("0123456789", _EXT_DIGITS)
LATIN_DIGITS = str.maketrans(_EXT_DIGITS + _ARABIC_DIGITS, "0123456789" * 2)


def to_latin_digits(text: str) -> str:
    return text.translate(LATIN_DIGITS)


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = to_latin_digits(text).strip().replace(",", "").replace("\u066c", "").replace(" ", "")
    if cleaned.lstrip("-").isdigit() and len(cleaned) <= 15:
        return int(cleaned)
    return None


def size(num_bytes: int | None) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}".replace(".00 ", " ")
        value /= 1024
    return f"{value:.2f} TB"


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + g_days[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


# Jalali month and weekday names, transliterated so bot code stays ASCII-only
# (test_no_persian_in_python_code); user-facing text still uses Persian digits.
JALALI_MONTHS = (
    "Farvardin",
    "Ordibehesht",
    "Khordad",
    "Tir",
    "Mordad",
    "Shahrivar",
    "Mehr",
    "Aban",
    "Azar",
    "Dey",
    "Bahman",
    "Esfand",
)
# Monday-first, matching datetime.weekday().
JALALI_WEEKDAYS = (
    "Doshanbeh",
    "Seshanbeh",
    "Chaharshanbeh",
    "Panjshanbeh",
    "Jomeh",
    "Shanbeh",
    "Yekshanbeh",
)


def jalali_month_name(month: int) -> str:
    """Transliterated Jalali month name, e.g. 1 -> "Farvardin"."""
    if not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError(f"invalid Jalali month: {month!r}")
    return JALALI_MONTHS[month - 1]


def jalali_weekday(dt: datetime | None) -> str:
    """Transliterated Jalali weekday name for a date, e.g. Saturdays -> "Shanbeh"."""
    if dt is None:
        return "-"
    return JALALI_WEEKDAYS[local(dt).weekday()]


def jstrftime_fa(dt: datetime | None, with_time: bool = False) -> str:
    """A Jalali date with weekday and month words and Persian digits.

    Example: "Shanbeh 1 Farvardin 1405" (digits rendered Persian).
    """
    if dt is None:
        return "-"
    loc = local(dt)
    jy, jm, jd = gregorian_to_jalali(loc.year, loc.month, loc.day)
    text = f"{JALALI_WEEKDAYS[loc.weekday()]} {jd} {JALALI_MONTHS[jm - 1]} {jy}"
    if with_time:
        text += f" {loc.hour:02d}:{loc.minute:02d}"
    return text.translate(PERSIAN_DIGITS)


def local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_settings().tz)


def jdate(dt: datetime | None, with_time: bool = False) -> str:
    if dt is None:
        return "-"
    loc = local(dt)
    jy, jm, jd = gregorian_to_jalali(loc.year, loc.month, loc.day)
    text = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if with_time:
        text += f" {loc.hour:02d}:{loc.minute:02d}"
    return text


def days_left(expire_at: datetime | None, now: datetime | None = None) -> int | None:
    if expire_at is None:
        return None
    now = now or datetime.now(UTC)
    seconds = (expire_at - now).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 86400) + (1 if seconds % 86400 else 0)


def processing_key(params: dict[str, object] | None) -> tuple[str, dict[str, str]]:
    """Locale key (and its values) telling the customer what happens to a paid order next."""
    until = (params or {}).get("queued_until")
    if (params or {}).get("queued") and until:
        return "renew_queued", {"date": jdate(datetime.fromisoformat(str(until)), with_time=True)}
    return "order_processing", {}
