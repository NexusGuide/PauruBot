"""Remnawave: REST API under /api, answers wrapped in {"response": ...}, users addressed by UUID (PNL-09).

Authentication is an API token made in the panel (Settings → API tokens), or the panel admin's
username and password. Users get internal squads instead of inbounds. Remnawave has no
"start on first connection" mode, so on-hold plans start counting at purchase here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    whole,
)

STATUS = {"ACTIVE": "active", "DISABLED": "disabled", "LIMITED": "limited", "EXPIRED": "expired"}
# Remnawave needs an end date for every user; "unlimited" is a date far away.
NEVER = datetime(2099, 12, 31, tzinfo=UTC)


def _tg(note: str) -> int | None:
    if note.startswith("tg:") and note[3:].isdigit():
        return int(note[3:])
    return None


class RemnawaveDriver(PanelDriver):
    kind = "remnawave"
    supports_webhooks = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = self.options.get("api_token") or None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        if self.base_url.startswith("http://"):
            # Remnawave refuses plain HTTP unless it looks like it came through a TLS proxy.
            headers.update({"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"})
        return headers

    async def _login(self) -> str:
        if self.options.get("api_token"):
            raise PanelError("API token rejected", 401)
        resp = await self.http.request(
            "POST",
            "/api/auth/login",
            json={"username": self.username, "password": self.password},
            headers={k: v for k, v in self._headers().items() if k != "Authorization"},
        )
        if resp.status_code not in (200, 201):
            raise PanelError("login failed", resp.status_code)
        try:
            token = as_dict(resp.json().get("response")).get("accessToken")
        except ValueError as exc:
            raise PanelError("login returned non-JSON") from exc
        if not token:
            raise PanelError("login response has no access token")
        self._token = str(token)
        return self._token

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._token is None:
            await self._login()
        for attempt in range(2):
            resp = await self.http.request(method, path, headers=self._headers(), **kwargs)
            if resp.status_code == 401 and attempt == 0 and not self.options.get("api_token"):
                await self._login()
                continue
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
            return data.get("response", data) if isinstance(data, dict) else data
        raise PanelError("unauthorized", 401)

    # --- mapping ------------------------------------------------------------

    def parse_user(self, data: dict[str, Any]) -> PanelUser:
        traffic = as_dict(data.get("userTraffic"))
        used = int(number(traffic, "usedTrafficBytes") or number(data, "usedTrafficBytes") or 0)
        expire = parse_expire(data.get("expireAt"))
        if expire is not None and expire.year >= 2099:
            expire = None
        return PanelUser(
            username=str(data.get("username", "")),
            status=STATUS.get(str(data.get("status") or "ACTIVE").upper(), "active"),
            used_bytes=used,
            data_limit=int(number(data, "trafficLimitBytes") or 0),
            expire_at=expire,
            subscription_url=self.absolute_url(data.get("subscriptionUrl")),
            data={k: str(data[k]) for k in ("uuid", "shortUuid") if data.get(k)},
            online_at=parse_expire(traffic.get("onlineAt") or data.get("onlineAt")),
            sub_agent=str(data["subLastUserAgent"]) if data.get("subLastUserAgent") else None,
            sub_fetched_at=parse_expire(data.get("subLastOpenedAt")),
            note=str(data.get("description") or ""),
        )

    async def _uuid(self, ref: PanelRef) -> str:
        if ref.data.get("uuid"):
            return str(ref.data["uuid"])
        found = await self.call("GET", f"/api/users/by-username/{seg(ref.username)}")
        uuid = as_dict(found).get("uuid")
        if not uuid:
            raise PanelNotFound(f"user {ref.username} not found", 404)
        return str(uuid)

    def squads(self, overrides: dict[str, Any] | None = None) -> list[str]:
        opts = {**self.options, **(overrides or {})}
        return [str(s) for s in opts.get("squad_ids") or []]

    @staticmethod
    def iso(value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        stats = as_dict(await self.call("GET", "/api/system/stats"))
        if not stats:
            return PanelHealth(False, "unexpected stats response")
        users = whole(number(as_dict(stats.get("users")), "totalUsers"))
        return PanelHealth(True, "ok", users, None)

    async def stats(self) -> dict[str, Any]:
        stats = as_dict(await self.call("GET", "/api/system/stats"))
        cpu, memory = as_dict(stats.get("cpu")), as_dict(stats.get("memory"))
        users, online = as_dict(stats.get("users")), as_dict(stats.get("onlineStats"))
        counts = as_dict(users.get("statusCounts"))
        figures: dict[str, Any] = {
            "version": None,
            "cpu_cores": whole(number(cpu, "cores")),
            "mem_used": whole(number(memory, "used")),
            "mem_total": whole(number(memory, "total")),
            "users_total": whole(number(users, "totalUsers")),
            "users_active": whole(number(counts, "ACTIVE")),
            "users_online": whole(number(online, "onlineNow")),
        }
        try:
            nodes = await self.call("GET", "/api/nodes")
        except PanelError:
            nodes = None
        rows = nodes if isinstance(nodes, list) else as_dict(nodes).get("nodes") or []
        figures["nodes"] = [
            {
                "id": n.get("uuid"),
                "name": str(n.get("name") or n.get("uuid")),
                "status": "disabled"
                if n.get("isDisabled")
                else ("connected" if n.get("isConnected") else "error"),
                "message": str(n.get("lastStatusMessage") or "")[:200] or None,
            }
            for n in rows
            if isinstance(n, dict)
        ]
        return figures

    async def list_options(self) -> list[PanelOption]:
        data = as_dict(await self.call("GET", "/api/internal-squads"))
        return [
            PanelOption(id=str(s["uuid"]), title=str(s.get("name") or s["uuid"]))
            for s in data.get("internalSquads") or []
            if isinstance(s, dict) and s.get("uuid")
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        expire = spec.expire_at
        if spec.on_hold_days:
            expire = datetime.now(UTC) + timedelta(days=int(spec.on_hold_days))
        payload: dict[str, Any] = {
            "username": spec.username,
            "status": "ACTIVE",
            "expireAt": self.iso(expire or NEVER),
            "trafficLimitBytes": int(spec.data_limit or 0),
            "trafficLimitStrategy": "NO_RESET",
            "description": spec.note,
            "activeInternalSquads": self.squads(spec.options),
        }
        if spec.ip_limit:
            payload["hwidDeviceLimit"] = int(spec.ip_limit)
        telegram_id = _tg(spec.note)
        if telegram_id:
            payload["telegramId"] = telegram_id
        return self.parse_user(as_dict(await self.call("POST", "/api/users", json=payload)))

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            if ref.data.get("uuid"):
                data = await self.call("GET", f"/api/users/{ref.data['uuid']}")
            else:
                data = await self.call("GET", f"/api/users/by-username/{seg(ref.username)}")
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
        uuid = await self._uuid(ref)
        payload: dict[str, Any] = {"uuid": uuid}
        if expire_at is not UNSET:
            payload["expireAt"] = self.iso(expire_at or NEVER)
        if data_limit is not None:
            payload["trafficLimitBytes"] = int(data_limit)
        data: Any = None
        if len(payload) > 1:
            data = await self.call("PATCH", "/api/users", json=payload)
        if enabled is not None:
            data = await self.call("POST", f"/api/users/{uuid}/actions/{'enable' if enabled else 'disable'}")
        if not isinstance(data, dict):
            data = await self.call("GET", f"/api/users/{uuid}")
        return self.parse_user(as_dict(data))

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call("POST", f"/api/users/{await self._uuid(ref)}/actions/reset-traffic")

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            await self.call("DELETE", f"/api/users/{await self._uuid(ref)}")
        except PanelNotFound:
            return  # already gone

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        uuid = await self._uuid(ref)
        data = await self.call("POST", f"/api/users/{uuid}/actions/revoke", json={})
        return self.parse_user(as_dict(data)) if isinstance(data, dict) else await self._fetch(uuid)

    async def _fetch(self, uuid: str) -> PanelUser:
        return self.parse_user(as_dict(await self.call("GET", f"/api/users/{uuid}")))

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        data = as_dict(await self.call("GET", "/api/users", params={"start": offset, "size": max(1, limit)}))
        rows = data.get("users") or []
        total = data.get("total")
        users = [self.parse_user(r) for r in rows if isinstance(r, dict) and r.get("username")]
        return users, int(total) if total is not None else None
