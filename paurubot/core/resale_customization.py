"""Validation helpers for reseller bot appearance settings."""

from __future__ import annotations

import re

TEXT_KEYS = {
    "customize_welcome": "welcome",
    "customize_rules": "terms_text",
    "customize_guide": "guide_text",
}
MAX_CUSTOM_TEXT = 4_000


def parse_channel(raw: str) -> tuple[str, str] | None:
    """Parse ``@channel | https://t.me/channel`` or return None for invalid input."""
    parts = [part.strip() for part in raw.split("|", 1)]
    if len(parts) != 2:
        return None
    chat_id, url = parts
    if not chat_id or not re.fullmatch(r"(?:@?[A-Za-z0-9_]{4,}|-100\d{5,})", chat_id):
        return None
    if not re.fullmatch(r"https://t\.me/[A-Za-z0-9_+/=-]{4,}", url):
        return None
    return chat_id if chat_id.startswith("@") or chat_id.startswith("-100") else "@" + chat_id, url
