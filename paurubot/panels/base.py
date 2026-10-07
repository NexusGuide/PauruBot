"""Panel driver protocol and shared value types (ARC-03)."""

from __future__ import annotations

import base64
import binascii
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from paurubot.panels.http import SafeHttpClient

GB = 1024**3


class PanelError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PanelNotFound(PanelError):
    pass


class PanelUnavailable(PanelError):
    """Network-level failure: safe to retry later."""


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


@dataclass(slots=True)
class UserSpec:
    username: str
    data_limit: int = 0
    expire_at: datetime | None = None
    note: str = ""
    ip_limit: int = 0
    on_hold_days: int | None = None
    # Per-plan overrides of the panel's options (groups, inbounds, flow, inbound_id) (PNL-14).
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PanelUser:
    username: str
    status: str = "active"
    used_bytes: int = 0
    data_limit: int = 0
    expire_at: datetime | None = None
    subscription_url: str | None = None
    links: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    online_at: datetime | None = None
    note: str = ""
    owner: str | None = None  # the panel admin the user belongs to, when the panel has admins


SUBSCRIPTION_MAX_BYTES = 2 * 1024 * 1024
# Headers client apps read from a subscription response; everything else is dropped.
SUBSCRIPTION_HEADERS = {
    "subscription-userinfo",
    "profile-update-interval",
    "profile-title",
    "content-disposition",
    "routing",
    "announce",
}


@dataclass(slots=True)
class SubscriptionBody:
    content: bytes
    content_type: str
    headers: dict[str, str]


@dataclass(slots=True)
class PanelRef:
    username: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PanelOption:
    """A selectable panel object (group, inbound) used to configure what new users get."""

    id: str
    title: str


@dataclass(slots=True)
class PanelHealth:
    ok: bool
    detail: str
    users: int | None = None
    version: str | None = None


def number(data: dict[str, Any], *keys: str) -> float | None:
    """The first numeric value among `keys` (panels name the same figure differently)."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def whole(value: float | None) -> int | None:
    return int(value) if value is not None else None


def system_figures(info: dict[str, Any]) -> dict[str, Any]:
    """Map Marzban / PasarGuard `/api/system` (and node stats) onto one shape."""
    cpu = number(info, "cpu_usage", "cpu_percent", "cpu")
    return {
        "version": str(info["version"]) if info.get("version") else None,
        "users_total": whole(number(info, "total_user", "total_users")),
        "users_active": whole(number(info, "users_active", "active_users")),
        "users_online": whole(number(info, "online_users", "users_online")),
        "cpu_percent": round(max(0.0, min(100.0, cpu)), 1) if cpu is not None else None,
        "cpu_cores": whole(number(info, "cpu_cores", "cores")),
        "mem_used": whole(number(info, "mem_used", "memory_used", "ram_used")),
        "mem_total": whole(number(info, "mem_total", "memory_total", "ram_total")),
        "traffic_in": whole(number(info, "incoming_bandwidth", "downlink", "incoming")),
        "traffic_out": whole(number(info, "outgoing_bandwidth", "uplink", "outgoing")),
        "speed_in": whole(number(info, "incoming_bandwidth_speed", "downlink_speed")),
        "speed_out": whole(number(info, "outgoing_bandwidth_speed", "uplink_speed")),
    }


def parse_expire(value: Any) -> datetime | None:
    if value in (None, 0, "0", ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10**11 else value
        if seconds <= 0:
            return None
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                return parse_expire(float(text))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def parse_ips(value: Any) -> list[str]:
    """Client addresses from the shapes panels use: a list of strings or {"ip": ...} objects,
    one string per line, or a JSON list inside a string; "1.2.3.4 (2024-01-01 10:00)" keeps the
    address. Anything that isn't an IP address is dropped; order is kept, duplicates removed."""
    import ipaddress
    import json

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                value = []
        else:
            value = text.splitlines()
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        raw = item.get("ip") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            continue
        candidate = raw.strip().split(" ", 1)[0].strip()
        try:
            address = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if address not in found:
            found.append(address)
    return found


def decode_subscription(raw: str) -> list[str]:
    body = raw.strip()
    if "://" not in body:
        padded = body + "=" * (-len(body) % 4)
        try:
            body = base64.b64decode(padded).decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError):
            return []
    return [line.strip() for line in body.splitlines() if "://" in line]


class PanelDriver(ABC):
    kind: str = ""
    supports_webhooks: bool = False

    def __init__(
        self, http: SafeHttpClient, username: str, password: str, options: dict[str, Any] | None = None
    ):
        self.http = http
        self.username = username
        self.password = password
        self.options: dict[str, Any] = dict(options or {})

    @property
    def base_url(self) -> str:
        return self.http.base_url

    async def close(self) -> None:
        await self.http.aclose()

    def absolute_url(self, url: str | None) -> str | None:
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    async def fetch_subscription(
        self, subscription_url: str | None, user_agent: str = "v2rayNG/1.8.0"
    ) -> SubscriptionBody | None:
        """Download a subscription as the client app would (through the SSRF guard)."""
        from urllib.parse import urlsplit

        from paurubot.panels.http import SafeHttpClient

        if not subscription_url:
            return None
        parts = urlsplit(subscription_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = subscription_url[len(origin) :] or "/"
        own = subscription_url.startswith(self.base_url + "/") or origin == self.base_url
        client = self.http if own else SafeHttpClient(origin, allow_private=self.http.allow_private)
        try:
            resp = await client.request("GET", path, headers={"User-Agent": user_agent[:200]})
        except PanelError:
            return None
        finally:
            if client is not self.http:
                await client.aclose()
        if resp.status_code != 200 or len(resp.content) > SUBSCRIPTION_MAX_BYTES:
            return None
        headers = {k: v for k, v in resp.headers.items() if k.lower() in SUBSCRIPTION_HEADERS}
        return SubscriptionBody(resp.content, resp.headers.get("content-type", "text/plain"), headers)

    async def fetch_links(self, subscription_url: str | None) -> list[str]:
        """Read a subscription and return its config links (best effort)."""
        body = await self.fetch_subscription(subscription_url)
        if body is None:
            return []
        return decode_subscription(body.content.decode("utf-8", "replace"))

    @abstractmethod
    async def health_check(self) -> PanelHealth: ...

    @abstractmethod
    async def list_options(self) -> list[PanelOption]: ...

    @abstractmethod
    async def create_user(self, spec: UserSpec) -> PanelUser: ...

    @abstractmethod
    async def get_user(self, ref: PanelRef) -> PanelUser | None: ...

    @abstractmethod
    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser: ...

    @abstractmethod
    async def reset_usage(self, ref: PanelRef) -> None: ...

    @abstractmethod
    async def delete_user(self, ref: PanelRef) -> None: ...

    @abstractmethod
    async def revoke_subscription(self, ref: PanelRef) -> PanelUser: ...

    async def stats(self) -> dict[str, Any]:
        """Resource, user, quota and node figures for monitoring (PNL-02, PNL-13). Best effort."""
        health = await self.health_check()
        return {"version": health.version, "users_total": health.users}

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        """A page of every user on the panel and the total when known (TEN-10)."""
        raise PanelError("listing users is not supported by this panel")

    supports_ips = False

    async def client_ips(self, ref: PanelRef) -> list[str]:
        """Addresses this user is connected from right now, in the panel's order (JOB-03)."""
        raise PanelError("this panel does not report client addresses")

    async def list_admins(self) -> list[str]:
        """Usernames of the panel's admins, for moving users between them (TEN-11)."""
        raise PanelError("this panel has no admins to move users between")

    async def set_owner(self, ref: PanelRef, admin: str) -> None:
        """Give a user to another panel admin (TEN-11)."""
        raise PanelError("this panel has no admins to move users between")
