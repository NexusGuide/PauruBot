"""Marzneshin: users get `service_ids`, expiry has a strategy and status is spread over flags (PNL-05)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelError,
    PanelHealth,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    number,
    parse_expire,
    seg,
    whole,
)
from paurubot.panels.drivers.token_api import TokenApiDriver


class MarzneshinDriver(TokenApiDriver):
    kind = "marzneshin"
    # /sub/{username}/{key}/{client_type} (F-32).
    sub_formats = {
        "v2ray": "v2ray",
        "clash": "clash",
        "clash-meta": "clash-meta",
        "sing-box": "sing-box",
        "xray-json": "xray",
    }
    # Marzneshin has no user webhooks; the sync job keeps usage fresh.
    supports_webhooks = False

    async def _login(self) -> str:
        resp = await self.http.request(
            "POST",
            "/api/admins/token",
            data={"username": self.username, "password": self.password, "grant_type": "password"},
        )
        if resp.status_code != 200:
            raise PanelError("login failed", resp.status_code)
        try:
            token = resp.json().get("access_token")
        except ValueError as exc:
            raise PanelError("login returned non-JSON") from exc
        if not token:
            raise PanelError("login response has no access token")
        self._token = str(token)
        return self._token

    def user_path(self, ref: PanelRef) -> str:
        return f"/api/users/{seg(ref.username)}"

    @staticmethod
    def iso(value: datetime) -> str:
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        used = int(data.get("used_traffic") or 0)
        limit = int(data.get("data_limit") or 0)
        strategy = str(data.get("expire_strategy") or "never")
        expire_at = parse_expire(data.get("expire_date")) if strategy == "fixed_date" else None
        if not data.get("enabled", True):
            status = "disabled"
        elif strategy == "start_on_first_use" and not data.get("activated", False):
            status = "on_hold"
        elif data.get("expired"):
            status = "expired"
        elif data.get("data_limit_reached") or (limit and used >= limit):
            status = "limited"
        else:
            status = "active"
        return PanelUser(
            username=str(data.get("username", "")),
            status=status,
            used_bytes=used,
            data_limit=limit,
            expire_at=expire_at,
            subscription_url=self.absolute_url(data.get("subscription_url")),
            links=list(data.get("links") or []),
            data={"id": data["id"]} if data.get("id") is not None else {},
            online_at=parse_expire(data.get("online_at")),
            sub_agent=str(data["sub_last_user_agent"]) if data.get("sub_last_user_agent") else None,
            sub_fetched_at=parse_expire(data.get("sub_updated_at")),
            note=str(data.get("note") or ""),
            owner=str(data["owner_username"]) if data.get("owner_username") else None,
        )

    def service_ids(self, overrides: dict[str, Any] | None = None) -> list[int]:
        opts = {**self.options, **(overrides or {})}
        return [int(s) for s in opts.get("service_ids") or opts.get("inbounds") or []]

    def expiry_fields(self, spec: UserSpec) -> dict[str, Any]:
        if spec.on_hold_days:
            return {
                "expire_strategy": "start_on_first_use",
                "expire_date": None,
                "usage_duration": int(spec.on_hold_days) * 86400,
            }
        if spec.expire_at:
            return {"expire_strategy": "fixed_date", "expire_date": self.iso(spec.expire_at)}
        return {"expire_strategy": "never", "expire_date": None}

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        stats = await self.call("GET", "/api/system/stats/users")
        if not isinstance(stats, dict):
            return PanelHealth(False, "unexpected stats response")
        total = stats.get("total")
        return PanelHealth(True, "ok", int(total) if total is not None else None, None)

    async def stats(self) -> dict[str, Any]:
        users = await self.call("GET", "/api/system/stats/users")
        users = users if isinstance(users, dict) else {}
        figures: dict[str, Any] = {
            "version": None,
            "users_total": whole(number(users, "total")),
            "users_active": whole(number(users, "active")),
            "users_online": whole(number(users, "online")),
        }
        try:
            nodes = await self.call("GET", "/api/nodes", params={"size": 100})
        except PanelError:
            nodes = None
        rows = nodes.get("items", []) if isinstance(nodes, dict) else nodes or []
        figures["nodes"] = [
            {
                "id": n.get("id"),
                "name": str(n.get("name") or n.get("id")),
                "status": str(n.get("status") or "unknown"),
                "message": str(n.get("message") or "")[:200] or None,
            }
            for n in rows
            if isinstance(n, dict)
        ]
        return figures

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/services", params={"size": 100})
        items = data.get("items", []) if isinstance(data, dict) else data or []
        return [PanelOption(id=str(s["id"]), title=str(s.get("name") or s["id"])) for s in items]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        payload: dict[str, Any] = {
            "username": spec.username,
            "service_ids": self.service_ids(spec.options),
            "data_limit": spec.data_limit,
            "data_limit_reset_strategy": "no_reset",
            "note": spec.note,
            **self.expiry_fields(spec),
        }
        data = await self.call("POST", "/api/users", json=payload)
        return self.parse_user(data)

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        payload: dict[str, Any] = {"username": ref.username}
        if expire_at is not UNSET:
            payload.update(
                {"expire_strategy": "fixed_date", "expire_date": self.iso(expire_at)}
                if expire_at
                else {"expire_strategy": "never", "expire_date": None}
            )
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        data: Any = None
        if len(payload) > 1:
            data = await self.call("PUT", self.user_path(ref), json=payload)
        if enabled is not None:
            data = await self.call("POST", f"{self.user_path(ref)}/{'enable' if enabled else 'disable'}")
        if not isinstance(data, dict):
            data = await self.call("GET", self.user_path(ref))
        return self.parse_user(data)

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        size = max(1, min(limit, 100))
        data = await self.call("GET", "/api/users", params={"page": offset // size + 1, "size": size})
        if isinstance(data, dict):
            rows, total = data.get("items") or [], data.get("total")
        else:
            rows, total = data or [], None
        users = [self.parse_user(r) for r in rows if isinstance(r, dict) and r.get("username")]
        return users, int(total) if total is not None else None

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self.call("GET", self.user_path(ref))
        except PanelNotFound:
            return None
        return self.parse_user(data)
