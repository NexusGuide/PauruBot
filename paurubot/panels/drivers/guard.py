"""Guard: subscriptions instead of users, an X-API-Key header and bulk actions by username (PNL-10).

Version 1 addresses one subscription at /api/subscriptions/{username}; version 2 drops that path
and works only with bulk calls and list filters. The panel option `guard_version` picks one.
`limit_expire` is a unix time, or a negative number of seconds for "start on first connection".
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
    whole,
)

TEN_YEARS = 10 * 365 * 86400


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class GuardDriver(PanelDriver):
    kind = "guard"
    supports_webhooks = False

    @property
    def v2(self) -> bool:
        return str(self.options.get("guard_version") or "v1").lower() == "v2"

    def _key(self) -> str:
        return str(self.options.get("api_token") or self.password or "")

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json", "X-API-Key": self._key()}
        resp = await self.http.request(method, path, headers=headers, **kwargs)
        if resp.status_code in (401, 403):
            raise PanelError("API key rejected", resp.status_code)
        if resp.status_code == 404:
            raise PanelNotFound(f"{method} {path} not found", 404)
        if resp.status_code >= 400:
            raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:300])
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # --- mapping ------------------------------------------------------------

    def _unwrap(self, data: Any) -> dict[str, Any]:
        for key in ("data", "subscription"):
            if isinstance(data, dict) and isinstance(data.get(key), (dict, list)):
                data = data[key]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data = data["items"]
        if isinstance(data, list):
            data = data[0] if data else {}
        return as_dict(data)

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        raw_expire = int(number(data, "limit_expire") or 0)
        on_hold = raw_expire < 0 or (bool(data.get("on_hold_timeout_days")) and not data.get("on_hold_since"))
        enabled = _bool(data.get("enabled", data.get("is_active")))
        if data.get("expired") or data.get("expired_at"):
            status = "expired"
        elif data.get("limited") or data.get("limited_at"):
            status = "limited"
        elif enabled is False:
            status = "disabled"
        elif on_hold:
            status = "on_hold"
        else:
            status = "active"
        url = data.get("subscription_url") or data.get("subscription_link") or data.get("link")
        if not url and data.get("tag") and data.get("access_key"):
            url = f"{self.base_url}/{data['tag']}/{data['access_key']}"
        expire = datetime.fromtimestamp(raw_expire, tz=UTC) if raw_expire > 0 else None
        if expire is not None and (expire - datetime.now(UTC)).total_seconds() > TEN_YEARS - 86400:
            expire = None  # the "no end date" we write is ten years ahead
        return PanelUser(
            username=str(data.get("username", "")),
            status=status,
            used_bytes=int(number(data, "total_usage", "usage", "used_traffic", "current_usage") or 0),
            data_limit=int(number(data, "limit_usage") or 0),
            expire_at=expire,
            subscription_url=self.absolute_url(str(url)) if url else None,
            data={"id": str(data["id"])} if data.get("id") is not None else {},
            online_at=parse_expire(data.get("online_at") or data.get("last_online_at")),
            note=str(data.get("note") or ""),
        )

    def _expire(self, spec: UserSpec) -> int:
        if spec.on_hold_days:
            return -int(spec.on_hold_days) * 86400
        if spec.expire_at:
            return int(spec.expire_at.timestamp())
        return int(datetime.now(UTC).timestamp()) + TEN_YEARS

    def services(self, overrides: dict[str, Any] | None = None) -> list[int]:
        opts = {**self.options, **(overrides or {})}
        return [int(s) for s in opts.get("service_ids") or []]

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        admin = as_dict(await self.call("GET", "/api/admins/current"))
        if not admin:
            return PanelHealth(False, "unexpected admin response")
        try:
            stats = as_dict(await self.call("GET", "/api/subscriptions/stats"))
        except PanelError:
            stats = {}
        return PanelHealth(True, "ok", whole(number(stats, "total", "total_subscriptions")), None)

    async def stats(self) -> dict[str, Any]:
        try:
            stats = as_dict(await self.call("GET", "/api/subscriptions/stats"))
        except PanelError:
            stats = {}
        return {
            "version": self.options.get("guard_version") or "v1",
            "users_total": whole(number(stats, "total", "total_subscriptions")),
            "users_active": whole(number(stats, "active", "enabled")),
            "users_online": whole(number(stats, "online")),
        }

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/services")
        rows = data.get("services") or data.get("items") or [] if isinstance(data, dict) else data or []
        return [
            PanelOption(id=str(s["id"]), title=str(s.get("remark") or s.get("name") or s["id"]))
            for s in rows
            if isinstance(s, dict) and s.get("id") is not None
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        services = self.services(spec.options)
        if not services:
            services = [int(o.id) for o in await self.list_options()]
        entry: dict[str, Any] = {
            "username": spec.username,
            "limit_usage": int(spec.data_limit or 0),
            "limit_expire": self._expire(spec),
            "services": services,
            "note": spec.note,
        }
        if spec.note.startswith("tg:"):
            entry["telegram_id"] = spec.note[3:]
        data = await self.call("POST", "/api/subscriptions", json=[entry])
        return self.parse_user(self._unwrap(data))

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            if self.v2:
                data = await self.call(
                    "GET", "/api/subscriptions", params={"usernames": ref.username, "limit": 1}
                )
            else:
                data = await self.call("GET", f"/api/subscriptions/{ref.username}")
        except PanelNotFound:
            return None
        user = self._unwrap(data)
        if not user or str(user.get("username", "")) != ref.username:
            return None
        return self.parse_user(user)

    async def _require(self, ref: PanelRef) -> PanelUser:
        user = await self.get_user(ref)
        if user is None:
            raise PanelNotFound(f"subscription {ref.username} not found", 404)
        return user

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
            payload["limit_expire"] = (
                int(expire_at.timestamp()) if expire_at else self._expire(UserSpec(ref.username))
            )
        if data_limit is not None:
            payload["limit_usage"] = int(data_limit)
        if payload:
            if self.v2:
                await self.call("PUT", "/api/subscriptions", json={**payload, "usernames": [ref.username]})
            else:
                await self.call("PUT", f"/api/subscriptions/{ref.username}", json=payload)
        if enabled is not None:
            action = "enable" if enabled else "disable"
            await self.call("POST", f"/api/subscriptions/{action}", json={"usernames": [ref.username]})
        return await self._require(ref)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call("POST", "/api/subscriptions/reset", json={"usernames": [ref.username]})

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.call("DELETE", "/api/subscriptions", json={"usernames": [ref.username]})
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        await self.call("POST", "/api/subscriptions/revoke", json={"usernames": [ref.username]})
        return await self._require(ref)

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        size = max(1, min(limit, 100))
        data = await self.call(
            "GET", "/api/subscriptions", params={"page": offset // size + 1, "size": size, "limit": size}
        )
        if isinstance(data, dict):
            rows, total = (
                data.get("items") or data.get("subscriptions") or data.get("data") or [],
                data.get("total"),
            )
        else:
            rows, total = data or [], None
        users = [self.parse_user(r) for r in rows if isinstance(r, dict) and r.get("username")]
        return users, int(total) if total is not None else None
