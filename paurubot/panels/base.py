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

    async def fetch_links(self, subscription_url: str | None) -> list[str]:
        """Read a subscription and return its config links (best effort, through the SSRF guard)."""
        from urllib.parse import urlsplit

        from paurubot.panels.http import SafeHttpClient

        if not subscription_url:
            return []
        parts = urlsplit(subscription_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = subscription_url[len(origin) :] or "/"
        own = subscription_url.startswith(self.base_url + "/") or origin == self.base_url
        client = self.http if own else SafeHttpClient(origin, allow_private=self.http.allow_private)
        try:
            resp = await client.request("GET", path, headers={"User-Agent": "v2rayNG/1.8.0"})
        except PanelError:
            return []
        finally:
            if client is not self.http:
                await client.aclose()
        if resp.status_code != 200:
            return []
        return decode_subscription(resp.text)

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
