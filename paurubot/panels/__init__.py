"""Panel registry: one long-lived client per configured panel, rebuilt when the panel changes."""

from __future__ import annotations

from datetime import datetime

from paurubot.db.models import Panel, PanelKind
from paurubot.panels.base import (
    GB,
    UNSET,
    PanelClient,
    PanelError,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
)
from paurubot.panels.drivers.marzban import MarzbanClient, PasarGuardClient
from paurubot.panels.drivers.xui import XuiClient

CLIENTS: dict[PanelKind, type[PanelClient]] = {
    PanelKind.PASARGUARD: PasarGuardClient,
    PanelKind.MARZBAN: MarzbanClient,
    PanelKind.XUI: XuiClient,
}

_registry: dict[int, tuple[datetime, PanelClient]] = {}


def build_client(panel: Panel) -> PanelClient:
    cls = CLIENTS[PanelKind(panel.kind)]
    from paurubot.security.secretbox import decrypt

    return cls(panel.base_url, panel.username, decrypt("panel", panel.password_enc), panel.options or {})


async def get_client(panel: Panel) -> PanelClient:
    cached = _registry.get(panel.id)
    stamp = panel.updated_at
    if cached and cached[0] == stamp:
        return cached[1]
    if cached:
        await cached[1].close()
    client = build_client(panel)
    _registry[panel.id] = (stamp, client)
    return client


async def drop_client(panel_id: int) -> None:
    cached = _registry.pop(panel_id, None)
    if cached:
        await cached[1].close()


async def close_all() -> None:
    for _, client in list(_registry.values()):
        await client.close()
    _registry.clear()


__all__ = [
    "GB",
    "UNSET",
    "PanelClient",
    "PanelError",
    "PanelNotFound",
    "PanelOption",
    "PanelRef",
    "PanelUser",
    "UserSpec",
    "build_client",
    "close_all",
    "drop_client",
    "get_client",
]
