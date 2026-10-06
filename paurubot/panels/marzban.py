"""Marzban and PasarGuard share the same token-based REST API with small schema differences."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from paurubot.panels.base import (
    UNSET,
    PanelClient,
    PanelError,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    parse_expire,
)


class TokenPanelClient(PanelClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None

    async def _login(self) -> str:
        try:
            resp = await self._client.post(
                "/api/admin/token",
                data={"username": self.username, "password": self.password, "grant_type": "password"},
            )
        except httpx.HTTPError as exc:
            raise PanelError(f"connection failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise PanelError("login failed", resp.status_code, resp.text[:500])
        token = resp.json().get("access_token")
        if not token:
            raise PanelError("login response has no access token")
        self._token = token
        return token

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._token is None:
            await self._login()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise PanelError(f"connection failed: {exc.__class__.__name__}") from exc
            if resp.status_code == 401 and attempt == 0:
                await self._login()
                continue
            if resp.status_code == 404:
                raise PanelNotFound(f"{method} {path} not found", 404, resp.text[:500])
            if resp.status_code >= 400:
                raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:500])
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise PanelError("unauthorized", 401)

    def _parse_user(self, data: dict[str, Any]) -> PanelUser:
        return PanelUser(
            username=data.get("username", ""),
            status=str(data.get("status") or "active"),
            used_bytes=int(data.get("used_traffic") or 0),
            data_limit=int(data.get("data_limit") or 0),
            expire_at=parse_expire(data.get("expire")),
            subscription_url=self.absolute_url(data.get("subscription_url")),
            links=list(data.get("links") or []),
            data={"id": data["id"]} if data.get("id") is not None else {},
            online_at=parse_expire(data.get("online_at")),
        )

    def _user_path(self, ref: PanelRef) -> str:
        return f"/api/user/{ref.username}"

    def _expire_value(self, expire_at: datetime | None) -> Any:
        return int(expire_at.timestamp()) if expire_at else 0

    async def test(self) -> str:
        info = await self.request("GET", "/api/system")
        version = info.get("version") if isinstance(info, dict) else None
        users = info.get("total_user") if isinstance(info, dict) else None
        return f"version={version or '?'} users={users if users is not None else '?'}"

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self.request("GET", self._user_path(ref))
        except PanelNotFound:
            return None
        return self._parse_user(data)

    async def modify_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        payload: dict[str, Any] = {}
        if expire_at is not UNSET:
            payload["expire"] = self._expire_value(expire_at)
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        if enabled is not None:
            payload["status"] = "active" if enabled else "disabled"
        data = await self.request("PUT", self._user_path(ref), json=payload)
        return self._parse_user(data)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.request("POST", f"{self._user_path(ref)}/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.request("DELETE", self._user_path(ref))
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        data = await self.request("POST", f"{self._user_path(ref)}/revoke_sub")
        return self._parse_user(data)


class MarzbanClient(TokenPanelClient):
    kind = "marzban"

    async def list_options(self) -> list[PanelOption]:
        data = await self.request("GET", "/api/inbounds")
        options: list[PanelOption] = []
        for protocol, inbounds in (data or {}).items():
            for inbound in inbounds or []:
                tag = inbound.get("tag")
                if tag:
                    options.append(PanelOption(id=f"{protocol}:{tag}", title=f"{protocol} · {tag}"))
        return options

    def _proxies_and_inbounds(self) -> tuple[dict[str, Any], dict[str, list[str]] | None]:
        selected: list[str] = list(self.options.get("inbounds") or [])
        if not selected:
            proxies = self.options.get("proxies") or {"vless": {}}
            return dict(proxies), None
        inbounds: dict[str, list[str]] = {}
        for item in selected:
            protocol, _, tag = str(item).partition(":")
            inbounds.setdefault(protocol, []).append(tag)
        proxies = {protocol: {} for protocol in inbounds}
        if "vless" in proxies and self.options.get("flow"):
            proxies["vless"] = {"flow": self.options["flow"]}
        return proxies, inbounds

    async def create_user(self, spec: UserSpec) -> PanelUser:
        proxies, inbounds = self._proxies_and_inbounds()
        payload: dict[str, Any] = {
            "username": spec.username,
            "proxies": proxies,
            "data_limit": spec.data_limit,
            "expire": self._expire_value(spec.expire_at),
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": spec.note,
        }
        if inbounds:
            payload["inbounds"] = inbounds
        data = await self.request("POST", "/api/user", json=payload)
        return self._parse_user(data)


class PasarGuardClient(TokenPanelClient):
    kind = "pasarguard"

    def _expire_value(self, expire_at: datetime | None) -> Any:
        if not expire_at:
            return 0
        return expire_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    async def _with_fallback(self, method: str, ref: PanelRef, suffix: str = "", **kwargs: Any) -> Any:
        """Newer PasarGuard builds address users by-username; older ones use /api/user/{name}."""
        try:
            return await self.request(method, f"/api/user/by-username/{ref.username}{suffix}", **kwargs)
        except PanelNotFound:
            return await self.request(method, f"/api/user/{ref.username}{suffix}", **kwargs)
        except PanelError as exc:
            if exc.status == 405:
                return await self.request(method, f"/api/user/{ref.username}{suffix}", **kwargs)
            raise

    async def list_options(self) -> list[PanelOption]:
        data = await self.request("GET", "/api/groups")
        groups = data.get("groups", []) if isinstance(data, dict) else data or []
        return [PanelOption(id=str(g["id"]), title=str(g.get("name") or g["id"])) for g in groups]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        group_ids = [int(g) for g in self.options.get("group_ids") or []]
        payload: dict[str, Any] = {
            "username": spec.username,
            "status": "active",
            "group_ids": group_ids,
            "proxy_settings": {},
            "data_limit": spec.data_limit,
            "expire": self._expire_value(spec.expire_at),
            "data_limit_reset_strategy": "no_reset",
            "note": spec.note,
        }
        data = await self.request("POST", "/api/user", json=payload)
        return self._parse_user(data)

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            data = await self._with_fallback("GET", ref)
        except PanelNotFound:
            return None
        return self._parse_user(data)

    async def modify_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        payload: dict[str, Any] = {}
        if expire_at is not UNSET:
            payload["expire"] = self._expire_value(expire_at)
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        if enabled is not None:
            payload["status"] = "active" if enabled else "disabled"
        data = await self._with_fallback("PUT", ref, json=payload)
        return self._parse_user(data)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self._with_fallback("POST", ref, "/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self._with_fallback("DELETE", ref)
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        data = await self._with_fallback("POST", ref, "/revoke_sub")
        return self._parse_user(data)
