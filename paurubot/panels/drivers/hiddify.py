"""Hiddify Manager: the v2 admin API under the admin proxy path, users by UUID (PNL-07).

The panel address is the admin link without its trailing UUID (https://host/<admin path>), and
the API key is the admin's UUID. Hiddify counts days from `start_date` (empty means "starts on the
first connection") and keeps usage in GB. The subscription address is the client link
(https://host/<client path>), set as the panel's subscription base.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from paurubot.panels.base import (
    GB,
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
    telegram_id_from_note,
)

UNLIMITED_GB = 100_000
UNLIMITED_DAYS = 10_000
USERS = "/api/v2/admin/user/"


class HiddifyDriver(PanelDriver):
    kind = "hiddify"
    supports_webhooks = False

    def _key(self) -> str:
        return str(self.options.get("api_token") or self.password or "")

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json", "Hiddify-API-Key": self._key()}
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

    def sub_url(self, uuid: str) -> str | None:
        base = str(self.options.get("sub_base_url") or "").rstrip("/")
        return f"{base}/{uuid}/" if base and uuid else None

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        limit_gb = number(data, "usage_limit_GB") or 0
        used_gb = number(data, "current_usage_GB") or 0
        days = int(number(data, "package_days") or 0)
        start = data.get("start_date")
        expire: datetime | None = None
        if start and 0 < days < UNLIMITED_DAYS:
            started = datetime.fromisoformat(str(start)[:10]).replace(tzinfo=UTC)
            expire = started + timedelta(days=days)
        limit = 0 if limit_gb >= UNLIMITED_GB else int(limit_gb * GB)
        used = int(used_gb * GB)
        if not data.get("enable", True):
            status = "disabled"
        elif not start and 0 < days < UNLIMITED_DAYS:
            status = "on_hold"
        elif limit and used >= limit:
            status = "limited"
        elif expire is not None and expire <= datetime.now(UTC):
            status = "expired"
        else:
            status = "active"
        uuid = str(data.get("uuid") or "")
        return PanelUser(
            username=str(data.get("name", "")),
            status=status,
            used_bytes=used,
            data_limit=limit,
            expire_at=expire,
            subscription_url=self.sub_url(uuid),
            data={"uuid": uuid} if uuid else {},
            online_at=parse_expire(data.get("last_online")),
            note=str(data.get("comment") or ""),
        )

    @staticmethod
    def _days_until(expire_at: datetime) -> int:
        return max(1, math.ceil((expire_at - datetime.now(UTC)).total_seconds() / 86400))

    async def _all(self) -> list[dict[str, Any]]:
        data = await self.call("GET", USERS)
        rows = data if isinstance(data, list) else as_dict(data).get("users") or []
        return [r for r in rows if isinstance(r, dict)]

    async def _raw(self, ref: PanelRef) -> dict[str, Any]:
        if ref.data.get("uuid"):
            return as_dict(await self.call("GET", f"{USERS}{ref.data['uuid']}/"))
        for row in await self._all():
            if row.get("name") == ref.username:
                return row
        raise PanelNotFound(f"user {ref.username} not found", 404)

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        status = await self.call("GET", "/api/v2/admin/server_status/")
        if not isinstance(status, dict):
            return PanelHealth(False, "unexpected status response")
        return PanelHealth(True, "ok", len(await self._all()), None)

    async def stats(self) -> dict[str, Any]:
        status = as_dict(await self.call("GET", "/api/v2/admin/server_status/"))
        system = as_dict(as_dict(status.get("stats")).get("system"))
        users = await self._all()
        return {
            "version": None,
            "cpu_percent": number(system, "cpu_percent"),
            "cpu_cores": number(system, "num_cpus"),
            "mem_used": int((number(system, "ram_used") or 0) * GB),
            "mem_total": int((number(system, "ram_total") or 0) * GB),
            "users_total": len(users),
            "users_active": sum(1 for u in users if u.get("enable", True)),
        }

    async def list_options(self) -> list[PanelOption]:
        return []  # every Hiddify user gets the panel's own proxies

    async def create_user(self, spec: UserSpec) -> PanelUser:
        payload: dict[str, Any] = {
            "name": spec.username,
            "usage_limit_GB": round(spec.data_limit / GB, 3) if spec.data_limit else UNLIMITED_GB,
            "current_usage_GB": 0,
            "mode": "no_reset",
            "enable": True,
            "comment": spec.note,
        }
        if spec.on_hold_days:
            payload.update(package_days=int(spec.on_hold_days), start_date=None)
        elif spec.expire_at:
            today_utc = datetime.now(UTC).date().isoformat()
            payload.update(package_days=self._days_until(spec.expire_at), start_date=today_utc)
        else:
            payload.update(package_days=UNLIMITED_DAYS, start_date=datetime.now(UTC).date().isoformat())
        if spec.note.startswith("tg:"):
            telegram_id = telegram_id_from_note(spec.note)
            if telegram_id is not None:
                payload["telegram_id"] = telegram_id
        return self.parse_user(as_dict(await self.call("POST", USERS, json=payload)))

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
        row = await self._raw(ref)
        payload: dict[str, Any] = {}
        if expire_at is not UNSET:
            payload["start_date"] = datetime.now(UTC).date().isoformat()
            payload["package_days"] = self._days_until(expire_at) if expire_at else UNLIMITED_DAYS
        if data_limit is not None:
            payload["usage_limit_GB"] = round(data_limit / GB, 3) if data_limit else UNLIMITED_GB
        if enabled is not None:
            payload["enable"] = enabled
        if payload:
            data = await self.call("PATCH", f"{USERS}{row['uuid']}/", json=payload)
            row = as_dict(data) or {**row, **payload}
        return self.parse_user(row)

    async def reset_usage(self, ref: PanelRef) -> None:
        row = await self._raw(ref)
        await self.call("PATCH", f"{USERS}{row['uuid']}/", json={"current_usage_GB": 0})

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            row = await self._raw(ref)
            await self.call("DELETE", f"{USERS}{row['uuid']}/")
        except PanelNotFound:
            return

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        """Hiddify links are the user's UUID, so a new link means a new user with the same settings."""
        row = await self._raw(ref)
        copy = {
            k: row.get(k)
            for k in (
                "name",
                "usage_limit_GB",
                "current_usage_GB",
                "package_days",
                "start_date",
                "mode",
                "enable",
                "comment",
                "telegram_id",
            )
            if k in row
        }
        created = as_dict(await self.call("POST", USERS, json=copy))
        await self.call("DELETE", f"{USERS}{row['uuid']}/")
        return self.parse_user(created)

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        rows = await self._all()
        page = rows[offset : offset + max(1, limit)]
        return [self.parse_user(r) for r in page if r.get("name")], len(rows)
