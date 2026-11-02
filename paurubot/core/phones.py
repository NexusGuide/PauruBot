"""One way to write a phone number, so the same number is always the same number (REF-04).

Telegram shares contacts as "989121234567", "+989121234567" or, from some clients and from
migrated bots, as "09121234567" or with Persian digits. Comparing those as text let one number
take one free trial per spelling.
"""

from __future__ import annotations

from paurubot.core.formatting import to_latin_digits

MAX_DIGITS = 15  # E.164


def normalize(raw: str | None) -> str | None:
    """International digits without "+" (e.g. "989121234567"), or None when it is not a number."""
    digits = "".join(ch for ch in to_latin_digits(raw or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("09"):
        digits = "98" + digits[1:]  # an Iranian mobile written the local way
    if not 7 <= len(digits) <= MAX_DIGITS:
        return None
    return digits


def allowed(raw: str | None, country_codes: list[str] | None) -> str | None:
    """The normalized number when the shop accepts it (its country codes, if any), else None."""
    phone = normalize(raw)
    codes = [str(code).lstrip("+") for code in country_codes or []]
    if phone is None or (codes and not any(phone.startswith(code) for code in codes)):
        return None
    return phone
