"""WG-Mate: a WireGuard and proxy panel with a plain REST API under /api (PNL-08).

Every request carries the panel's API key in X-API-Key. Users are addressed by id; quota and usage
are bytes and the end date is ISO UTC. The protocols and server a new user gets are copied from a
template user named in the panel's settings, or set per plan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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
)


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


class WGMateDriver(PanelDriver):
    kind = "wgmate"
    supports_webhooks = False

    def _key(self) -> str:
        return str(self.options.get("api_token") or self.password or "")

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json", "X-API-Key": self._key()}
        resp = await self.http.request(method, f"/api{path}", headers=headers, **kwargs)
        if resp.status_code in (401, 403):
            raise PanelError("API key rejected", resp.status_code)
        if resp.status_code == 404:
            raise PanelNotFound(f"{method} {path} not found", 404)
        if resp.status_code >= 400:
            raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:300])
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError as exc:
            raise PanelError("unexpected non-JSON response", resp.status_code) from exc
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # --- mapping ------------------------------------------------------------

    def sub_url(self, token: str | None) -> str | None:
        return f"{self.base_url}/sub/{token}" if token else None

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        raw = str(data.get("status") or "active")
        status = raw
        if raw == "disabled" and data.get("disableReason") == "user_quota":
            status = "limited"
        expire = parse_expire(data.get("expiresAt"))
        limit = int(number(data, "quotaBytes") or 0)
        used = int(number(data, "usedBytes") or 0)
        if status == "active" and expire is not None and expire <= datetime.now(UTC):
            status = "expired"
        elif status == "active" and limit and used >= limit:
            status = "limited"
        online = (
            datetime.now(UTC)
            if data.get("connection") == "online"
            else parse_expire(data.get("lastHandshakeAt"))
        )
        return PanelUser(
            username=str(data.get("username", "")),
            status=status,
            used_bytes=used,
            data_limit=limit,
            expire_at=expire,
            subscription_url=self.sub_url(data.get("subToken")),
            data={"id": str(data["id"])} if data.get("id") is not None else {},
            online_at=online,
            note=str(data.get("note") or ""),
        )

    async def _raw(self, ref: PanelRef) -> dict[str, Any]:
        return as_dict(await self.call("GET", f"/users/by-username/{quote(ref.username, safe='')}"))

    async def _id(self, ref: PanelRef) -> str:
        if ref.data.get("id"):
            return str(ref.data["id"])
        row = await self._raw(ref)
        if row.get("id") is None:
            raise PanelNotFound(f"user {ref.username} not found", 404)
        return str(row["id"])

    async def _template(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """protocols and serverId, from the plan, the panel options, or a named template user."""
        opts = {**self.options, **overrides}
        found: dict[str, Any] = {k: opts[k] for k in ("protocols", "serverId") if opts.get(k)}
        if len(found) < 2 and opts.get("template_user"):
            row = await self._raw(PanelRef(str(opts["template_user"])))
            for key in ("protocols", "serverId"):
                if key not in found and row.get(key) is not None:
                    found[key] = row[key]
        return found

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        await self.call("GET", "/auth/me")
        return PanelHealth(True, "ok", None, None)

    async def list_options(self) -> list[PanelOption]:
        return []

    async def create_user(self, spec: UserSpec) -> PanelUser:
        payload: dict[str, Any] = {
            "username": spec.username,
            "note": spec.note,
            "quotaBytes": int(spec.data_limit or 0),
            "expiresAt": _iso(spec.expire_at),
            **await self._template(spec.options),
        }
        if spec.ip_limit:
            payload["connLimit"] = int(spec.ip_limit)
        data = as_dict(await self.call("POST", "/users", json=payload))
        if data.get("id") is None:
            raise PanelError("panel did not return the new user")
        return self.parse_user({**payload, **data})

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            return self.parse_user(await self._raw(ref))
        except PanelNotFound:
            return None

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        user_id = await self._id(ref)
        payload: dict[str, Any] = {}
        if expire_at is not UNSET:
            payload["expiresAt"] = _iso(expire_at)
        if data_limit is not None:
            payload["quotaBytes"] = int(data_limit)
        if payload:
            await self.call("PATCH", f"/users/{user_id}", json=payload)
        row = await self._raw(ref)
        quota_stop = row.get("status") == "disabled" and row.get("disableReason") == "user_quota"
        if enabled is True or (payload and quota_stop):
            await self.call("POST", f"/users/{user_id}/enable")
        elif enabled is False:
            await self.call("POST", f"/users/{user_id}/disable")
        if enabled is not None or quota_stop:
            row = await self._raw(ref)
        return self.parse_user(row)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call("POST", f"/users/{await self._id(ref)}/reset")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.call("DELETE", f"/users/{await self._id(ref)}")
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        await self.call("POST", f"/users/{await self._id(ref)}/revoke")
        return self.parse_user(await self._raw(ref))
