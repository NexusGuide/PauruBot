"""Client app downloads, kept current from GitHub releases (BOT-05).

GitHub is often filtered for customers, so when a cache chat is configured each release asset
small enough for the Bot API is uploaded to Telegram once and then re-sent by file id.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from paurubot import kv

log = logging.getLogger(__name__)

GITHUB = "https://api.github.com"
PLATFORMS = ("android", "ios", "windows", "mac", "linux")
TELEGRAM_LIMIT = 49 * 1024 * 1024
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
_URL = re.compile(r"^https://[^\s]{4,300}$")


class AppError(ValueError):
    pass


def validate(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 20:
        raise AppError("download_apps_invalid")
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise AppError("download_apps_invalid")
        name = str(item.get("name") or "").strip()
        platform = str(item.get("platform") or "")
        repo = str(item.get("repo") or "").strip()
        pattern = str(item.get("pattern") or "").strip()
        store = str(item.get("store_url") or "").strip()
        if not name or len(name) > 32 or platform not in PLATFORMS:
            raise AppError("download_apps_invalid")
        if repo and not _REPO.match(repo):
            raise AppError("download_apps_repo_invalid")
        if store and not _URL.match(store):
            raise AppError("download_apps_url_invalid")
        if not repo and not store:
            raise AppError("download_apps_invalid")
        clean.append(
            {"name": name, "platform": platform, "repo": repo, "pattern": pattern[:64], "store_url": store}
        )
    return clean


@dataclass(slots=True)
class Release:
    version: str
    asset: str
    url: str
    size: int


def pick_asset(assets: list[dict[str, Any]], pattern: str) -> dict[str, Any] | None:
    for asset in assets:
        name = str(asset.get("name") or "")
        if not pattern or fnmatch.fnmatch(name.lower(), pattern.lower()):
            return asset
    return None


async def latest(
    repo: str, pattern: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> Release | None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), transport=transport) as client:
        resp = await client.get(
            f"{GITHUB}/repos/{repo}/releases/latest", headers={"Accept": "application/vnd.github+json"}
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    asset = pick_asset(list(data.get("assets") or []), pattern)
    if asset is None:
        return None
    return Release(
        str(data.get("tag_name") or data.get("name") or ""),
        str(asset["name"]),
        str(asset["browser_download_url"]),
        int(asset.get("size") or 0),
    )


def cache_key(shop_id: int, app: dict[str, str]) -> str:
    selection = json.dumps([app.get("repo"), app.get("name"), app.get("pattern", "")], separators=(",", ":"))
    digest = hashlib.sha256(selection.encode()).hexdigest()[:24]
    return f"apps:v2:{shop_id}:{app['platform']}:{digest}"


async def cached(shop_id: int, app: dict[str, str]) -> dict[str, Any] | None:
    raw = await kv.get_redis().get(cache_key(shop_id, app))
    return json.loads(raw) if raw else None


async def refresh(
    shop_id: int,
    apps: list[dict[str, str]],
    cache_chat_id: int | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Look up each app's latest release and, when possible, mirror its file to Telegram."""
    from aiogram.types import FSInputFile

    from paurubot.notifications.telegram import get_bot

    updated = 0
    for app in apps:
        if not app.get("repo"):
            continue
        try:
            release = await latest(app["repo"], app.get("pattern", ""), transport=transport)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("release lookup failed for %s: %s", app["repo"], exc.__class__.__name__)
            continue
        if release is None:
            continue
        previous = await cached(shop_id, app) or {}
        entry: dict[str, Any] = {
            "version": release.version,
            "asset": release.asset,
            "url": release.url,
            "size": release.size,
            "file_id": previous.get("file_id")
            if previous.get("version") == release.version
            and previous.get("asset") == release.asset
            and previous.get("url") == release.url
            else None,
        }
        if cache_chat_id and not entry["file_id"] and 0 < release.size <= TELEGRAM_LIMIT:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / release.asset
                try:
                    async with (
                        httpx.AsyncClient(
                            timeout=httpx.Timeout(120, connect=10), follow_redirects=True, transport=transport
                        ) as client,
                        client.stream("GET", release.url) as resp,
                    ):
                        resp.raise_for_status()
                        received = 0
                        with path.open("wb") as handle:
                            async for chunk in resp.aiter_bytes():
                                received += len(chunk)
                                if received > TELEGRAM_LIMIT:
                                    raise AppError("download_too_large")
                                handle.write(chunk)
                    message = await get_bot().send_document(
                        cache_chat_id,
                        FSInputFile(path, filename=release.asset),
                        caption=f"{app['name']} {release.version}",
                        disable_notification=True,
                    )
                    if message.document is not None:
                        entry["file_id"] = message.document.file_id
                except Exception as exc:  # the link still works without the mirror
                    log.warning("mirroring %s failed: %s", release.asset, exc.__class__.__name__)
        await kv.get_redis().set(cache_key(shop_id, app), json.dumps(entry), ex=7 * 86_400)
        updated += 1
    return updated
