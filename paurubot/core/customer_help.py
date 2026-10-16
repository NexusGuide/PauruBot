"""Read-only, allowlisted Mini App help/download projection of this shop's existing settings."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

PLATFORMS = ("android", "ios", "windows", "mac", "linux")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BAD_ESCAPE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", re.I)


def safe_url(value: Any) -> str | None:
    """Navigation only, never fetched/proxied here. Fail closed on ambiguous/non-public URL forms.

    This is syntactic filtering, not domain ownership, DNS/redirect checking or a malware guarantee.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    if (
        any(ord(char) <= 32 or ord(char) >= 127 for char in value)
        or "\\" in value
        or _BAD_ESCAPE.search(value)
    ):
        return None
    if not value.lower().startswith("https://"):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        return None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]*", labels[-1]):
        return None
    if labels[-1] in {"local", "localhost", "internal"}:
        return None
    return value


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def bot_url(username: str | None) -> str | None:
    value = (username or "").lstrip("@")
    return f"https://t.me/{value}" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value) else None


def view(cfg: dict[str, Any], *, bot_username: str | None) -> dict[str, Any]:
    """Only help fields; never dump settings, cached Telegram file IDs or payment credentials."""
    articles = []
    raw_articles = cfg.get("help_items")
    for index, item in enumerate(raw_articles[:30] if isinstance(raw_articles, list) else []):
        if not isinstance(item, dict):
            continue
        title, text = _text(item.get("title"), 128), _text(item.get("text"), 8000)
        url = safe_url(item.get("url"))
        if title and (text or url):
            articles.append({"id": index, "title": title, "text": text, "url": url})
    downloads = []
    raw_apps = cfg.get("download_apps")
    for index, item in enumerate(raw_apps[:20] if isinstance(raw_apps, list) else []):
        if not isinstance(item, dict):
            continue
        name, platform = _text(item.get("name"), 32), item.get("platform")
        if not name or platform not in PLATFORMS:
            continue
        store = safe_url(item.get("store_url"))
        repo = _text(item.get("repo"), 160)
        release = f"https://github.com/{repo}/releases" if _REPO.fullmatch(repo) else None
        if store or release:
            downloads.append(
                {"id": index, "name": name, "platform": platform, "store_url": store, "release_url": release}
            )
    return {"articles": articles, "apps": downloads, "bot_url": bot_url(bot_username)}
