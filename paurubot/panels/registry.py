"""One long-lived driver per panel row, rebuilt whenever the row changes (ARC-03)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from paurubot.db.models import Panel, PanelKind
from paurubot.panels.base import PanelDriver
from paurubot.panels.drivers.guard import GuardDriver
from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.drivers.marzneshin import MarzneshinDriver
from paurubot.panels.drivers.pasarguard import PasarGuardDriver
from paurubot.panels.drivers.rebecca import RebeccaDriver
from paurubot.panels.drivers.remnawave import RemnawaveDriver
from paurubot.panels.drivers.xui import XuiDriver
from paurubot.panels.http import SafeHttpClient
from paurubot.security.secretbox import decrypt

DRIVERS: dict[PanelKind, type[PanelDriver]] = {
    PanelKind.PASARGUARD: PasarGuardDriver,
    PanelKind.MARZBAN: MarzbanDriver,
    PanelKind.XUI: XuiDriver,
    PanelKind.MARZNESHIN: MarzneshinDriver,
    PanelKind.REMNAWAVE: RemnawaveDriver,
    PanelKind.REBECCA: RebeccaDriver,
    PanelKind.GUARD: GuardDriver,
}

_cache: dict[int, tuple[str, PanelDriver]] = {}
# Tests install a mock transport per panel id.
test_transports: dict[int, httpx.AsyncBaseTransport] = {}


def build(panel: Panel, *, transport: httpx.AsyncBaseTransport | None = None) -> PanelDriver:
    options: dict[str, Any] = dict(panel.options or {})
    if options.get("api_token_enc"):
        options["api_token"] = decrypt("panel", options.pop("api_token_enc"))
    http = SafeHttpClient(
        panel.base_url,
        pinned_pem=panel.tls_cert_pem,
        allow_private=panel.allow_private_address,
        transport=transport or test_transports.get(panel.id),
    )
    driver_cls = DRIVERS[PanelKind(panel.kind)]
    return driver_cls(http, panel.username, decrypt("panel", panel.password_enc), options)


def fingerprint(panel: Panel) -> str:
    """What the driver is built from; health and stats updates on the row must not rebuild it."""
    parts = [
        str(panel.kind),
        panel.base_url,
        panel.username,
        panel.password_enc,
        json.dumps(panel.options or {}, sort_keys=True, default=str),
        panel.tls_cert_pem or "",
        str(bool(panel.allow_private_address)),
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


async def driver_for(panel: Panel) -> PanelDriver:
    key = fingerprint(panel)
    cached = _cache.get(panel.id)
    if cached and cached[0] == key:
        return cached[1]
    if cached:
        await cached[1].close()
    driver = build(panel)
    _cache[panel.id] = (key, driver)
    return driver


async def drop(panel_id: int) -> None:
    cached = _cache.pop(panel_id, None)
    if cached:
        await cached[1].close()


async def close_all() -> None:
    for _, driver in list(_cache.values()):
        await driver.close()
    _cache.clear()
