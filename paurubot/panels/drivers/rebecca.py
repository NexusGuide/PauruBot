"""Rebecca: a Marzban descendant with services and a v2 user API (PNL-10).

Sign-in is an API key (sent as a bearer token) or the admin's username and password through the
Marzban-style token endpoint. Users are created and changed through /api/v2/users with one
`service_id`; reading, resetting, revoking and deleting use the classic /api/user paths.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelError,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    parse_ips,
)
from paurubot.panels.drivers.marzban import MarzbanDriver


class RebeccaDriver(MarzbanDriver):
    kind = "rebecca"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.options.get("api_token"):
            self._token = str(self.options["api_token"])

    async def _login(self) -> str:
        if self.options.get("api_token"):
            raise PanelError("API key rejected", 401)
        return await super()._login()

    def service_id(self, overrides: dict[str, Any] | None = None) -> int | None:
        opts = {**self.options, **(overrides or {})}
        ids = opts.get("service_ids") or []
        return int(ids[0]) if ids else None

    supports_ips = True

    async def client_ips(self, ref: PanelRef) -> list[str]:
        """Rebecca's access insights list each user's live source addresses."""
        data = await self.call("GET", "/api/core/access/insights")
        if isinstance(data, dict):
            data = data.get("items") or (data.get("data") or {}).get("items") or []
        found: list[str] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or ref.username not in (
                item.get("user_key"),
                item.get("user_label"),
            ):
                continue
            for address in parse_ips(item.get("sources") or []):
                if address not in found:
                    found.append(address)
        return found

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/v2/services")
        rows = data.get("services") if isinstance(data, dict) else data
        return [
            PanelOption(id=str(s["id"]), title=str(s.get("name") or s["id"]))
            for s in rows or []
            if isinstance(s, dict) and s.get("id") is not None
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        payload: dict[str, Any] = {
            "username": spec.username,
            "data_limit": int(spec.data_limit or 0),
            "note": spec.note,
            "ip_limit": max(0, int(spec.ip_limit or 0)),
            **self.lifecycle_fields(spec),
        }
        service = self.service_id(spec.options)
        if service is not None:
            payload["service_id"] = service
        else:
            proxies, inbounds = self.proxies_and_inbounds(spec.options)
            payload["proxies"] = proxies
            if inbounds:
                payload["inbounds"] = inbounds
        try:
            data = await self.call("POST", "/api/v2/users", json=payload)
        except PanelNotFound:
            data = await self.call("POST", "/api/user", json=payload)  # older builds
        return self.parse_user(data)

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
            payload["expire"] = self.expire_value(expire_at)
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        if enabled is not None:
            payload["status"] = "active" if enabled else "disabled"
        try:
            data = await self.call("PUT", f"/api/v2/users/{ref.username}", json=payload)
        except PanelNotFound:
            data = await self.call("PUT", self.user_path(ref), json=payload)
        return self.parse_user(data)
