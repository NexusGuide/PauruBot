"""Shared client for the bearer-token REST APIs of Marzban and PasarGuard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelDriver,
    PanelError,
    PanelHealth,
    PanelNotFound,
    PanelRef,
    PanelUser,
    UserSpec,
    number,
    parse_expire,
    seg,
    system_figures,
    usage_counter,
    whole,
)


class TokenApiDriver(PanelDriver):
    supports_webhooks = True
    parallel_reads = 3  # the token login below is single-flight

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        # Single-flight login (F-05): concurrent calls on a cold or expired token share one login
        # instead of each posting the password and tripping the panel's rate limit.
        self._login_lock = asyncio.Lock()

    async def _login(self) -> str:
        resp = await self.http.request(
            "POST",
            "/api/admin/token",
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

    async def _fresh_token(self, stale: str | None = None) -> str:
        """The current token, logging in only when there is none or it is still the rejected one."""
        async with self._login_lock:
            if self._token is not None and self._token != stale:
                return self._token
            return await self._login()

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        token = self._token or await self._fresh_token()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await self.http.request(method, path, headers=headers, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                token = await self._fresh_token(stale=token)
                continue
            if resp.status_code == 404:
                raise PanelNotFound(f"{method} {path} not found", 404)
            if resp.status_code >= 400:
                raise PanelError(
                    f"{method} {path} failed ({resp.status_code}): {reason(resp)}",
                    resp.status_code,
                    resp.text[:300],
                )
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise PanelError("unauthorized", 401)

    # --- mapping ------------------------------------------------------------

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        return PanelUser(
            username=str(data.get("username", "")),
            status=str(data.get("status") or "active"),
            used_bytes=int(data.get("used_traffic") or 0),
            usage_known=usage_counter(data.get("used_traffic")) is not None,
            data_limit=int(data.get("data_limit") or 0),
            expire_at=parse_expire(data.get("expire")),
            subscription_url=self.absolute_url(data.get("subscription_url")),
            links=list(data.get("links") or []),
            data={"id": data["id"]} if data.get("id") is not None else {},
            online_at=parse_expire(data.get("online_at")),
            note=str(data.get("note") or ""),
            owner=_owner(data.get("admin")),
        )

    async def list_admins(self) -> list[str]:
        data = await self.call("GET", "/api/admins", params={"limit": 500})
        rows = (data.get("admins") or data.get("items")) if isinstance(data, dict) else data
        return sorted(str(r["username"]) for r in rows or [] if isinstance(r, dict) and r.get("username"))

    async def set_owner(self, ref: PanelRef, admin: str) -> None:
        await self.call("PUT", f"{self.user_path(ref)}/set-owner", params={"admin_username": admin})

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        data = await self.call("GET", "/api/users", params={"offset": offset, "limit": limit})
        if isinstance(data, dict):
            rows, total = data.get("users") or [], data.get("total")
        else:
            rows, total = data or [], None
        users = [self.parse_user(row) for row in rows if isinstance(row, dict) and row.get("username")]
        return users, int(total) if total is not None else None

    def expire_value(self, expire_at: datetime | None) -> Any:
        return int(expire_at.timestamp()) if expire_at else 0

    def user_path(self, ref: PanelRef) -> str:
        return f"/api/user/{seg(ref.username)}"

    def lifecycle_fields(self, spec: UserSpec) -> dict[str, Any]:
        if spec.on_hold_days:
            return {
                "status": "on_hold",
                "expire": 0,
                "on_hold_expire_duration": int(spec.on_hold_days) * 86400,
            }
        return {"status": "active", "expire": self.expire_value(spec.expire_at)}

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        info = await self.call("GET", "/api/system")
        if not isinstance(info, dict):
            return PanelHealth(False, "unexpected system response")
        users = info.get("total_user")
        version = info.get("version")
        return PanelHealth(
            True, "ok", int(users) if users is not None else None, str(version) if version else None
        )

    async def stats(self) -> dict[str, Any]:
        info = await self.call("GET", "/api/system")
        figures = system_figures(info if isinstance(info, dict) else {})
        figures["nodes"] = await self.nodes()
        return figures

    async def nodes(self) -> list[dict[str, Any]]:
        try:
            data = await self.call("GET", "/api/nodes")
        except PanelError:
            return []
        rows = data.get("nodes", []) if isinstance(data, dict) else data or []
        return [
            {
                "id": node.get("id"),
                "name": str(node.get("name") or node.get("id")),
                "status": str(node.get("status") or "unknown"),
                "message": str(node.get("message") or "")[:200] or None,
                "uplink": whole(number(node, "uplink")),
                "downlink": whole(number(node, "downlink")),
            }
            for node in rows
            if isinstance(node, dict)
        ]

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self.call("GET", self.user_path(ref))
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
        data = await self.call("PUT", self.user_path(ref), json=payload)
        return self.parse_user(data)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call("POST", f"{self.user_path(ref)}/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.call("DELETE", self.user_path(ref))
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        data = await self.call("POST", f"{self.user_path(ref)}/revoke_sub")
        return self.parse_user(data)

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)


def _owner(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("username")
    return str(value) if value else None


def reason(resp: Any) -> str:
    """The panel's own short explanation of a refusal (FastAPI `detail`), for staff to read."""
    try:
        body = resp.json()
    except ValueError:
        return " ".join(resp.text.split())[:200] or "no details"
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, list):  # validation errors: [{"loc": [...], "msg": "..."}]
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail[:3])
    return " ".join(str(detail).split())[:200] or "no details"
