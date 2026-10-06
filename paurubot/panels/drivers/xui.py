"""3x-ui (Sanaei / Alireza forks) driver over the inbound client API (PNL-06)."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from paurubot.panels.base import (
    UNSET,
    PanelDriver,
    PanelError,
    PanelHealth,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    as_dict,
    number,
    parse_expire,
    whole,
)

SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan"}


class XuiDriver(PanelDriver):
    kind = "xui"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._logged_in = False
        self._protocols: dict[int, str] = {}

    @property
    def inbound_id(self) -> int:
        return self.inbound_for({})

    def inbound_for(self, overrides: dict[str, Any]) -> int:
        value = overrides.get("inbound_id") or self.options.get("inbound_id")
        if not value:
            raise PanelError("inbound_id is not configured for this panel")
        return int(value)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        token = self.options.get("api_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _login(self) -> None:
        if self.options.get("api_token"):
            self._logged_in = True
            return
        resp = await self.http.request(
            "POST", "/login", data={"username": self.username, "password": self.password}
        )
        ok = False
        try:
            ok = bool(resp.json().get("success"))
        except ValueError:
            ok = False
        if resp.status_code != 200 or not ok:
            raise PanelError("login failed", resp.status_code)
        self._logged_in = True

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._logged_in:
            await self._login()
        for attempt in range(2):
            resp = await self.http.request(method, path, headers=self._headers(), **kwargs)
            # An expired session shows up as a redirect to the login page, 401/404, or an HTML 200.
            not_json = "application/json" not in resp.headers.get("content-type", "")
            expired = resp.status_code in {301, 302, 401, 404} or (resp.status_code == 200 and not_json)
            if expired and attempt == 0 and not self.options.get("api_token"):
                self._logged_in = False
                await self._login()
                continue
            if resp.status_code >= 400:
                raise PanelError(f"{method} {path} failed", resp.status_code)
            try:
                body = resp.json()
            except ValueError as exc:
                raise PanelError("unexpected non-JSON response", resp.status_code) from exc
            if not body.get("success", False):
                raise PanelError(str(body.get("msg") or f"{method} {path} rejected"), resp.status_code)
            return body.get("obj")
        raise PanelError("session could not be established")

    async def _protocol(self, inbound_id: int) -> str:
        if inbound_id not in self._protocols:
            inbound = await self.call("GET", f"/panel/api/inbounds/get/{inbound_id}")
            protocol = str((inbound or {}).get("protocol", "")).lower()
            if protocol not in SUPPORTED_PROTOCOLS:
                raise PanelError(f"inbound protocol '{protocol}' is not supported")
            self._protocols[inbound_id] = protocol
        return self._protocols[inbound_id]

    def subscription_url(self, sub_id: str | None) -> str | None:
        base = str(self.options.get("sub_base_url") or "").rstrip("/")
        if not base or not sub_id:
            return None
        return f"{base}/{sub_id}"

    @staticmethod
    def client_key(protocol: str, client: dict[str, Any]) -> str:
        return str(client["password"] if protocol == "trojan" else client["id"])

    @staticmethod
    def _expiry_ms(expire_at: datetime | None) -> int:
        return int(expire_at.timestamp() * 1000) if expire_at else 0

    def build_client(self, protocol: str, spec: UserSpec) -> dict[str, Any]:
        expiry = self._expiry_ms(spec.expire_at)
        if spec.on_hold_days:
            expiry = -int(spec.on_hold_days) * 86400 * 1000  # 3x-ui: negative = starts on first use
        client: dict[str, Any] = {
            "email": spec.username,
            "enable": True,
            "limitIp": int(
                spec.ip_limit or spec.options.get("limit_ip") or self.options.get("limit_ip") or 0
            ),
            "totalGB": int(spec.data_limit),
            "expiryTime": expiry,
            "tgId": "",
            "subId": secrets.token_hex(8),
            "reset": 0,
            "comment": spec.note[:120] if spec.note else "",
        }
        if protocol == "trojan":
            client["password"] = secrets.token_urlsafe(12)
        else:
            client["id"] = str(uuid.uuid4())
        flow = spec.options.get("flow") or self.options.get("flow")
        if protocol == "vless" and flow:
            client["flow"] = flow
        if protocol == "vmess":
            client["security"] = "auto"
        return client

    def to_user(self, data: dict[str, Any], traffic: dict[str, Any] | None) -> PanelUser:
        client = data["client"]
        used = 0
        enabled = bool(client.get("enable", True))
        total = int(client.get("totalGB") or 0)
        expiry = int(client.get("expiryTime") or 0)
        if traffic:
            used = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
            enabled = bool(traffic.get("enable", enabled))
            total = int(traffic.get("total") or total)
            expiry = int(traffic.get("expiryTime", expiry) or 0)
        expire_at = parse_expire(expiry) if expiry > 0 else None
        if expiry < 0:
            status = "on_hold"
        elif not enabled:
            status = "disabled"
        else:
            status = "active"
        if total and used >= total:
            status = "limited"
        elif expire_at and expire_at <= datetime.now(UTC):
            status = "expired"
        return PanelUser(
            username=str(client["email"]),
            status=status,
            used_bytes=used,
            data_limit=total,
            expire_at=expire_at,
            subscription_url=self.subscription_url(client.get("subId")),
            data=data,
        )

    async def health_check(self) -> PanelHealth:
        inbounds = await self.call("GET", "/panel/api/inbounds/list")
        clients = sum(len(ib.get("clientStats") or []) for ib in inbounds or [])
        return PanelHealth(True, f"{len(inbounds or [])} inbounds", clients, None)

    async def stats(self) -> dict[str, Any]:
        status = await self.call("POST", "/server/status")
        status = as_dict(status)
        mem, traffic = as_dict(status.get("mem")), as_dict(status.get("netTraffic"))
        speed, xray = as_dict(status.get("netIO")), as_dict(status.get("xray"))
        cpu = number(status, "cpu")
        inbounds = await self.call("GET", "/panel/api/inbounds/list") or []
        return {
            "version": xray.get("version"),
            "users_total": sum(len(ib.get("clientStats") or []) for ib in inbounds),
            "cpu_percent": round(cpu, 1) if cpu is not None else None,
            "cpu_cores": whole(number(status, "cpuCores", "logicalPro")),
            "mem_used": whole(number(mem, "current")),
            "mem_total": whole(number(mem, "total")),
            "traffic_in": whole(number(traffic, "recv")),
            "traffic_out": whole(number(traffic, "sent")),
            "speed_in": whole(number(speed, "down")),
            "speed_out": whole(number(speed, "up")),
            "xray_state": xray.get("state"),
            "nodes": [],
        }

    async def list_options(self) -> list[PanelOption]:
        inbounds = await self.call("GET", "/panel/api/inbounds/list")
        return [
            PanelOption(
                id=str(ib["id"]),
                title=f"{ib.get('remark') or ib['id']} · {ib.get('protocol')}:{ib.get('port')}",
            )
            for ib in inbounds or []
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        inbound_id = self.inbound_for(spec.options)
        protocol = await self._protocol(inbound_id)
        client = self.build_client(protocol, spec)
        await self.call(
            "POST",
            "/panel/api/inbounds/addClient",
            data={"id": str(inbound_id), "settings": json.dumps({"clients": [client]})},
        )
        return self.to_user({"inbound_id": inbound_id, "protocol": protocol, "client": client}, None)

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        users: list[PanelUser] = []
        for inbound in await self.call("GET", "/panel/api/inbounds/list") or []:
            try:
                settings = json.loads(inbound.get("settings") or "{}")
            except ValueError:
                continue
            stats = {str(s.get("email")): s for s in inbound.get("clientStats") or [] if isinstance(s, dict)}
            for client in settings.get("clients") or []:
                if not client.get("email"):
                    continue
                data = {"inbound_id": inbound["id"], "protocol": inbound.get("protocol"), "client": client}
                user = self.to_user(data, stats.get(str(client["email"])))
                user.note = str(client.get("comment") or client.get("tgId") or "")
                users.append(user)
        return users[offset : offset + limit], len(users)

    async def _traffic(self, email: str) -> dict[str, Any] | None:
        return await self.call("GET", f"/panel/api/inbounds/getClientTraffics/{email}")

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        if "client" not in ref.data:
            return None
        traffic = await self._traffic(ref.username)
        if traffic is None:
            return None
        return self.to_user(ref.data, traffic)

    async def _update(self, ref: PanelRef, client: dict[str, Any], old_key: str | None = None) -> None:
        protocol = ref.data["protocol"]
        key = old_key or self.client_key(protocol, client)
        await self.call(
            "POST",
            f"/panel/api/inbounds/updateClient/{key}",
            data={"id": str(ref.data["inbound_id"]), "settings": json.dumps({"clients": [client]})},
        )

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        client = dict(ref.data["client"])
        if expire_at is not UNSET:
            client["expiryTime"] = self._expiry_ms(expire_at)
        if data_limit is not None:
            client["totalGB"] = int(data_limit)
        if enabled is not None:
            client["enable"] = bool(enabled)
        await self._update(ref, client)
        data = {**ref.data, "client": client}
        return self.to_user(data, await self._traffic(ref.username))

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.call(
            "POST", f"/panel/api/inbounds/{ref.data['inbound_id']}/resetClientTraffic/{ref.username}"
        )

    async def delete_user(self, ref: PanelRef) -> None:
        if "client" not in ref.data:
            return
        key = self.client_key(ref.data["protocol"], ref.data["client"])
        try:
            await self.call("POST", f"/panel/api/inbounds/{ref.data['inbound_id']}/delClient/{key}")
        except PanelError as exc:
            if "not found" not in str(exc).lower():
                raise

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        protocol = ref.data["protocol"]
        old_key = self.client_key(protocol, ref.data["client"])
        client = dict(ref.data["client"])
        client["subId"] = secrets.token_hex(8)
        if protocol == "trojan":
            client["password"] = secrets.token_urlsafe(12)
        else:
            client["id"] = str(uuid.uuid4())
        await self._update(ref, client, old_key=old_key)
        data = {**ref.data, "client": client}
        return self.to_user(data, await self._traffic(ref.username))
