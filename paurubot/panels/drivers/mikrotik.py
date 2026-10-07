"""MikroTik RouterOS 7 User Manager over the REST API (PNL-11).

Customers get a username and password for whatever the router serves with User Manager
(PPP, L2TP, hotspot…). What a plan gives is a User Manager profile: its validity and transfer
limits are set on the router, so the bot sells and renews by assigning that profile again and
only reads usage back. The profile comes from the plan's panel options, else the panel's.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import datetime
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
    number,
)

log = logging.getLogger(__name__)
UM = "/rest/user-manager"


def new_password() -> str:
    return secrets.token_hex(6)


class MikrotikDriver(PanelDriver):
    kind = "mikrotik"
    supports_webhooks = False
    delivery = "account"

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {"Accept": "application/json", "Authorization": f"Basic {token}"}

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self.http.request(method, path, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            raise PanelError("login failed", 401)
        if resp.status_code == 404:
            raise PanelNotFound(f"{method} {path} not found", 404)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("detail") or body.get("message") or "")
            except ValueError:
                detail = resp.text[:200]
            raise PanelError(detail or f"{method} {path} failed", resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise PanelError("unexpected non-JSON response", resp.status_code) from exc

    # --- users ----------------------------------------------------------------

    async def _row(self, ref: PanelRef) -> dict[str, Any]:
        rows = await self.call("GET", f"{UM}/user", params={"name": ref.username})
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("name") == ref.username:
                return row
        raise PanelNotFound(f"user {ref.username} not found", 404)

    async def _usage(self, user_id: str) -> int:
        try:
            rows = await self.call("POST", f"{UM}/user/monitor", json={"once": True, ".id": user_id})
        except PanelError:
            return 0
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        return int((number(row, "total-upload") or 0) + (number(row, "total-download") or 0))

    async def _parse(self, row: dict[str, Any], profile: str | None = None) -> PanelUser:
        disabled = str(row.get("disabled", "false")).lower() in {"true", "yes"}
        data: dict[str, Any] = {"id": str(row.get(".id") or ""), "password": str(row.get("password") or "")}
        if profile:
            data["profile"] = profile
        return PanelUser(
            username=str(row.get("name") or ""),
            status="disabled" if disabled else "active",
            used_bytes=await self._usage(data["id"]) if data["id"] else 0,
            data=data,
            limits_known=False,
        )

    def _profile(self, overrides: dict[str, Any] | None = None, ref: PanelRef | None = None) -> str:
        # The plan's choice, then what this user was sold, then the panel's default.
        profile = str(
            (overrides or {}).get("profile")
            or (ref.data.get("profile") if ref else "")
            or self.options.get("profile")
            or ""
        )
        if not profile:
            raise PanelError("no User Manager profile is set for this plan or panel")
        return profile

    async def _assign(self, username: str, profile: str) -> None:
        await self.call("POST", f"{UM}/user-profile/add", json={"user": username, "profile": profile})

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        info = await self.call("GET", "/rest/system/resource")
        version = info.get("version") if isinstance(info, dict) else None
        return PanelHealth(True, "ok", None, str(version) if version else None)

    async def stats(self) -> dict[str, Any]:
        info = await self.call("GET", "/rest/system/resource")
        info = info if isinstance(info, dict) else {}
        total, free = number(info, "total-memory") or 0, number(info, "free-memory") or 0
        return {
            "version": info.get("version"),
            "cpu_percent": number(info, "cpu-load"),
            "cpu_cores": number(info, "cpu-count"),
            "mem_used": int(total - free),
            "mem_total": int(total),
        }

    async def list_options(self) -> list[PanelOption]:
        rows = await self.call("GET", f"{UM}/profile")
        return (
            [
                PanelOption(id=str(r["name"]), title=str(r["name"]))
                for r in rows
                if isinstance(r, dict) and r.get("name")
            ]
            if isinstance(rows, list)
            else []
        )

    async def create_user(self, spec: UserSpec) -> PanelUser:
        profile = self._profile(spec.options)
        password = new_password()
        await self.call("POST", f"{UM}/user/add", json={"name": spec.username, "password": password})
        await self._assign(spec.username, profile)
        return await self._parse(await self._row(PanelRef(spec.username)), profile)

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            return await self._parse(await self._row(ref), ref.data.get("profile"))
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
        row = await self._row(ref)
        if expire_at is not UNSET or data_limit is not None:
            # More time or data means another period of the profile, queued after the current one.
            await self._assign(ref.username, self._profile(ref=ref))
        if enabled is not None:
            await self.call(
                "POST", f"{UM}/user/set", json={".id": row[".id"], "disabled": "no" if enabled else "yes"}
            )
            row = await self._row(ref)
        return await self._parse(row, ref.data.get("profile"))

    async def reset_usage(self, ref: PanelRef) -> None:
        # Usage is counted per profile period on the router; a new period starts from zero.
        log.debug("MikroTik usage resets with each profile period (%s)", ref.username)

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            row = await self._row(ref)
        except PanelNotFound:
            return
        await self.call("POST", f"{UM}/user/remove", json={".id": row[".id"]})

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        """New credentials: the same account with a fresh password."""
        row = await self._row(ref)
        await self.call("POST", f"{UM}/user/set", json={".id": row[".id"], "password": new_password()})
        return await self._parse(await self._row(ref), ref.data.get("profile"))

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        rows = await self.call("GET", f"{UM}/user")
        rows = [r for r in rows if isinstance(r, dict) and r.get("name")] if isinstance(rows, list) else []
        page = rows[offset : offset + max(1, limit)]
        return [
            PanelUser(
                username=str(r["name"]),
                status="disabled" if str(r.get("disabled", "")).lower() in {"true", "yes"} else "active",
                data={"id": str(r.get(".id") or ""), "password": str(r.get("password") or "")},
                limits_known=False,
            )
            for r in page
        ], len(rows)
