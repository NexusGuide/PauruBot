"""One long-lived driver per panel row, rebuilt whenever the row changes (ARC-03)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from paurubot.db.models import Panel, PanelKind
from paurubot.panels.base import PanelDriver
from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.drivers.pasarguard import PasarGuardDriver
from paurubot.panels.drivers.xui import XuiDriver
from paurubot.panels.http import SafeHttpClient
from paurubot.security.secretbox import decrypt

DRIVERS: dict[PanelKind, type[PanelDriver]] = {
    PanelKind.PASARGUARD: PasarGuardDriver,
    PanelKind.MARZBAN: MarzbanDriver,
    PanelKind.XUI: XuiDriver,
}

_cache: dict[int, tuple[datetime, PanelDriver]] = {}
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


async def driver_for(panel: Panel) -> PanelDriver:
    cached = _cache.get(panel.id)
    if cached and cached[0] == panel.updated_at:
        return cached[1]
    if cached:
        await cached[1].close()
    driver = build(panel)
    _cache[panel.id] = (panel.updated_at, driver)
    return driver


async def drop(panel_id: int) -> None:
    cached = _cache.pop(panel_id, None)
    if cached:
        await cached[1].close()


async def close_all() -> None:
    for _, driver in list(_cache.values()):
        await driver.close()
    _cache.clear()
