"""Nexora panel driver: options-based API-key REST API (competitor parity).

Configuration lives in the panel's ``options`` dict:

- ``api_key``: key issued by the panel, sent as ``X-API-Key`` (required; every
  call raises ``PanelError("not_configured")`` until it is set).
- ``base_url``: informational override; requests use the panel's own base URL.

Users live under versioned ``/api/v1`` paths and share the generic user shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelDriver,
    PanelError,
    PanelHealth,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    as_dict,
    number,
    parse_expire,
    seg,
)


class NexoraDriver(PanelDriver):
    kind = "nexora"

    def _key(self) -> str:
        key = str(self.options.get("api_key") or "")
        if not key:
            raise PanelError("not_configured: set the panel api_key option")
        return key

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-API-Key": self._key()}

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        self._key()
        resp = await self.http.request(method, path, headers=self._headers(), **kwargs)
        if resp.status_code in (401, 403):
            raise PanelError("API key rejected", resp.status_code)
        if resp.status_code == 404:
            raise PanelNotFound(f"{method} {path} not found", 404)
        if resp.status_code >= 400:
            raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:300])
        if not resp.content:
            return None
        try:
            data = resp.json()
        except ValueError:
            return resp.text
        return data.get("data", data) if isinstance(data, dict) else data

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        return PanelUser(
            username=str(data.get("username", "")),
            status=str(data.get("status") or "active"),
            used_bytes=int(number(data, "used_bytes", "used_traffic") or 0),
            data_limit=int(number(data, "data_limit") or 0),
            expire_at=parse_expire(data.get("expire_at", data.get("expire"))),
            subscription_url=self.absolute_url(data.get("subscription_url")),
            data={"id": str(data["id"])} if data.get("id") is not None else {},
            note=str(data.get("note") or ""),
        )

    def _payload(self, spec: UserSpec) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "username": spec.username,
            "data_limit": int(spec.data_limit or 0),
            "note": spec.note,
        }
        if spec.expire_at:
            payload["expire_at"] = spec.expire_at.astimezone(UTC).isoformat()
        if spec.on_hold_days:
            payload["on_hold_days"] = int(spec.on_hold_days)
        if spec.ip_limit:
            payload["ip_limit"] = int(spec.ip_limit)
        return payload

    async def health_check(self) -> PanelHealth:
        self._key()
        info = as_dict(await self.call("GET", "/api/v1/health"))
        users = number(info, "users", "total_users", "total")
        version = info.get("version")
        return PanelHealth(
            True, "ok", int(users) if users is not None else None, str(version) if version else None
        )

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/v1/services")
        rows = data if isinstance(data, list) else []
        return [
            PanelOption(id=str(s["id"]), title=str(s.get("name") or s.get("title") or s["id"]))
            for s in rows
            if isinstance(s, dict) and s.get("id") is not None
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        data = await self.call("POST", "/api/v1/users", json=self._payload(spec))
        return self.parse_user(as_dict(data))

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self.call("GET", f"/api/v1/users/{seg(ref.username)}")
        except PanelNotFound:
            return None
        return self.parse_user(as_dict(data))

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        payload: dict[str, Any] = {}
        if expire_at is not UNSET:
            payload["expire_at"] = expire_at.astimezone(UTC).isoformat() if expire_at else None
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        if enabled is not None:
            payload["status"] = "active" if enabled else "disabled"
        data = await self.call("PUT", f"/api/v1/users/{seg(ref.username)}", json=payload)
        return self.parse_user(as_dict(data))

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call("POST", f"/api/v1/users/{seg(ref.username)}/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.call("DELETE", f"/api/v1/users/{seg(ref.username)}")
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        data = await self.call("POST", f"/api/v1/users/{seg(ref.username)}/revoke", json={})
        if isinstance(data, dict):
            return self.parse_user(data)
        fetched = await self.get_user(ref)
        if fetched is None:
            raise PanelNotFound(f"user {ref.username} not found", 404)
        return fetched
