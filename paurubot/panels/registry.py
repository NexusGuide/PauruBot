"""One long-lived driver per panel row, rebuilt whenever the row changes (ARC-03)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import httpx

from paurubot.db.models import Panel, PanelKind
from paurubot.panels.base import ALL_CHANGES, PanelDriver, PanelError
from paurubot.panels.drivers.guard import GuardDriver
from paurubot.panels.drivers.hiddify import HiddifyDriver
from paurubot.panels.drivers.ibsng import IbsngDriver
from paurubot.panels.drivers.marzban import MarzbanDriver
from paurubot.panels.drivers.marzneshin import MarzneshinDriver
from paurubot.panels.drivers.mikrotik import MikrotikDriver
from paurubot.panels.drivers.mirza_agent import MirzaAgentDriver
from paurubot.panels.drivers.nexora import NexoraDriver
from paurubot.panels.drivers.pasarguard import PasarGuardDriver
from paurubot.panels.drivers.rebecca import RebeccaDriver
from paurubot.panels.drivers.remnawave import RemnawaveDriver
from paurubot.panels.drivers.wgdashboard import WGDashboardDriver
from paurubot.panels.drivers.wgmate import WGMateDriver
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
    PanelKind.HIDDIFY: HiddifyDriver,
    PanelKind.WGDASHBOARD: WGDashboardDriver,
    PanelKind.WGMATE: WGMateDriver,
    PanelKind.MIKROTIK: MikrotikDriver,
    PanelKind.IBSNG: IbsngDriver,
}

#: Competitor kinds resolved by name, without a PanelKind enum member (options-based
#: stub drivers; their rows keep the kind string in `panels.options["kind"]`-style setups
#: or any caller that passes the kind as a plain string).
EXTRA_DRIVERS: dict[str, type[PanelDriver]] = {
    MirzaAgentDriver.kind: MirzaAgentDriver,
    NexoraDriver.kind: NexoraDriver,
}

#: Panel option marking a panel as the emergency/failover target.
EMERGENCY_OPTION = "is_emergency"
#: Panel option restricting a panel to the national network.
NATIONAL_NET_OPTION = "national_net_only"


def driver_class_for(kind: PanelKind | str | None) -> type[PanelDriver] | None:
    """Driver class for a panel kind, including the options-registered competitor kinds."""
    if kind is None:
        return None
    key = kind.value if isinstance(kind, PanelKind) else str(kind)
    try:
        cls = DRIVERS.get(PanelKind(key))
    except ValueError:
        cls = None
    return cls or EXTRA_DRIVERS.get(key)


def is_emergency(panel: Panel) -> bool:
    """Whether the panel is an emergency/failover target (`is_emergency` option)."""
    return bool((panel.options or {}).get(EMERGENCY_OPTION))


def is_national(panel: Panel) -> bool:
    """Whether the panel serves the national network only (`national_net_only` option)."""
    return bool((panel.options or {}).get(NATIONAL_NET_OPTION))


def emergency_panels(panels: Iterable[Panel]) -> list[Panel]:
    """The emergency/failover panels among `panels` (those with the `is_emergency` option)."""
    return [panel for panel in panels if is_emergency(panel)]


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
    driver_cls = driver_class_for(panel.kind)
    if driver_cls is None:
        raise PanelError(f"unsupported panel kind {panel.kind!r}")
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


def delivery_of(kind: PanelKind | str | None) -> str:
    """How customers of this panel kind connect: subscription, wireguard or account (PNL-08, PNL-11)."""
    cls = driver_class_for(kind)
    return getattr(cls, "delivery", "subscription") if cls else "subscription"


def changes_of(kind: PanelKind | str | None) -> frozenset[str]:
    """What a sold account on this panel kind can still be changed with (renew, extras, gifts...)."""
    cls = driver_class_for(kind)
    return frozenset(getattr(cls, "changes", ALL_CHANGES)) if cls else ALL_CHANGES
