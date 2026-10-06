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
)
from paurubot.panels.drivers.token_api import TokenApiDriver

# Read-only admin endpoints the admin panel may browse through the driver (PNL-01).
ADMIN_READ_PATHS = {
    "system": "/api/system",
    "users": "/api/users",
    "admins": "/api/admins",
    "groups": "/api/groups",
    "hosts": "/api/hosts",
    "nodes": "/api/nodes",
    "node_stats": "/api/nodes/realtime_stats",
    "templates": "/api/user_templates",
    "inbounds": "/api/inbounds",
}


class PasarGuardDriver(TokenApiDriver):
    """PasarGuard: users belong to groups; expiry is ISO-8601 (PNL-01)."""

    kind = "pasarguard"

    def expire_value(self, expire_at: datetime | None) -> Any:
        if not expire_at:
            return 0
        return expire_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    async def _user_call(self, method: str, ref: PanelRef, suffix: str = "", **kwargs: Any) -> Any:
        """Newer builds address users by-username; older ones use /api/user/{name}."""
        if self.options.get("legacy_paths"):
            return await self.call(method, f"/api/user/{ref.username}{suffix}", **kwargs)
        try:
            return await self.call(method, f"/api/user/by-username/{ref.username}{suffix}", **kwargs)
        except PanelNotFound:
            return await self.call(method, f"/api/user/{ref.username}{suffix}", **kwargs)
        except PanelError as exc:
            if exc.status == 405:
                return await self.call(method, f"/api/user/{ref.username}{suffix}", **kwargs)
            raise

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/groups")
        groups = data.get("groups", []) if isinstance(data, dict) else data or []
        return [PanelOption(id=str(g["id"]), title=str(g.get("name") or g["id"])) for g in groups]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        template_id = self.options.get("template_id")
        if template_id:
            data = await self.call(
                "POST",
                "/api/user/from_template",
                json={"username": spec.username, "user_template_id": int(template_id), "note": spec.note},
            )
            user = self.parse_user(data)
            if spec.data_limit or spec.expire_at:
                user = await self.update_user(
                    PanelRef(user.username or spec.username, user.data),
                    expire_at=spec.expire_at,
                    data_limit=spec.data_limit,
                )
            return user
        payload: dict[str, Any] = {
            "username": spec.username,
            "group_ids": [int(g) for g in self.options.get("group_ids") or []],
            "proxy_settings": {},
            "data_limit": spec.data_limit,
            "data_limit_reset_strategy": "no_reset",
            "note": spec.note,
            **self.lifecycle_fields(spec),
        }
        if spec.ip_limit:
            payload["hwid_limit"] = int(spec.ip_limit)
        data = await self.call("POST", "/api/user", json=payload)
        return self.parse_user(data)

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self._user_call("GET", ref)
        except PanelNotFound:
            return None
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
        return self.parse_user(await self._user_call("PUT", ref, json=payload))

    async def reset_usage(self, ref: PanelRef) -> None:
        await self._user_call("POST", ref, "/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self._user_call("DELETE", ref)
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        return self.parse_user(await self._user_call("POST", ref, "/revoke_sub"))

    # --- admin views (PNL-01) ----------------------------------------------

    async def admin_read(self, resource: str, params: dict[str, Any] | None = None) -> Any:
        path = ADMIN_READ_PATHS.get(resource)
        if path is None:
            raise PanelError(f"unknown resource {resource}")
        clean = {
            k: v for k, v in (params or {}).items() if k in {"offset", "limit", "search", "status", "sort"}
        }
        return await self.call("GET", path, params=clean or None)

    async def node_reconnect(self, node_id: int) -> Any:
        return await self.call("POST", f"/api/node/{int(node_id)}/reconnect")

    async def node_sync(self, node_id: int) -> Any:
        return await self.call("PUT", f"/api/node/{int(node_id)}/sync")
