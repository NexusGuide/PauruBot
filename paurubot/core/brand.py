"""A shop's own look in its Mini App (REF-08).

A preset accent colour (never a free colour, so text always stays readable in light and dark),
the bot's Telegram profile photo as the logo, and a short notice on the home screen. Each reseller
bot is its own shop, so each brands its own Mini App.
"""

from __future__ import annotations

from typing import Any

# "" keeps the default blue; the palettes live in frontend/src/app/app.css ([data-accent=...]).
ACCENTS = ("", "green", "purple", "orange", "rose", "teal")
NOTICE_MAX = 300
LOGO_MAX_BYTES = 512 * 1024
LOGO_TTL = 6 * 3600


def clean_notice(value: Any) -> str:
    """One paragraph of plain text: whitespace folded, at most NOTICE_MAX characters."""
    text = " ".join(str(value or "").split())
    return text[:NOTICE_MAX]


def view(cfg: dict[str, Any]) -> dict[str, Any]:
    accent = str(cfg.get("miniapp_accent") or "")
    return {
        "accent": accent if accent in ACCENTS and accent else None,
        "logo": bool(cfg.get("miniapp_logo")),
        "notice": clean_notice(cfg.get("miniapp_notice")) or None,
    }
