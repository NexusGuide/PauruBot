"""3x-ui (Sanaei) panel client using the session-cookie inbound API."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime
from typing import Any

import httpx

from paurubot.panels.base import (
    UNSET,
    PanelClient,
    PanelError,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
    parse_expire,
)

SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan"}


class XuiClient(PanelClient):
    kind = "xui"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._logged_in = False
        self._protocols: dict[int, str] = {}

    @property
    def inbound_id(self) -> int:
        value = self.options.get("inbound_id")
        if not value:
            raise PanelError("inbound_id is not configured for this panel")
        return int(value)

    async def _login(self) -> None:
        try:
            resp = await self._client.post(
                "/login", data={"username": self.username, "password": self.password}
            )
        except httpx.HTTPError as exc:
            raise PanelError(f"connection failed: {exc.__class__.__name__}") from exc
        ok = False
        try:
            ok = bool(resp.json().get("success"))
        except ValueError:
            ok = False
        if resp.status_code != 200 or not ok:
            raise PanelError("login failed", resp.status_code, resp.text[:300])
        self._logged_in = True

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._logged_in:
            await self._login()
        for attempt in range(2):
            try:
                resp = await self._client.request(
                    method, path, headers={"Accept": "application/json"}, **kwargs
                )
            except httpx.HTTPError as exc:
                raise PanelError(f"connection failed: {exc.__class__.__name__}") from exc
            expired = resp.status_code in {301, 302, 401, 404} or "application/json" not in resp.headers.get(
                "content-type", ""
            )
            if expired and attempt == 0:
                await self._login()
                continue
            if resp.status_code >= 400:
                raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:300])
            try:
                body = resp.json()
            except ValueError as exc:
                raise PanelError("unexpected non-JSON response", resp.status_code) from exc
            if not body.get("success", False):
                raise PanelError(body.get("msg") or f"{method} {path} rejected", resp.status_code)
            return body.get("obj")
        raise PanelError("session could not be established")

    async def _protocol(self, inbound_id: int) -> str:
        if inbound_id not in self._protocols:
            inbound = await self.request("GET", f"/panel/api/inbounds/get/{inbound_id}")
            protocol = str((inbound or {}).get("protocol", "")).lower()
            if protocol not in SUPPORTED_PROTOCOLS:
                raise PanelError(f"inbound protocol '{protocol}' is not supported")
            self._protocols[inbound_id] = protocol
        return self._protocols[inbound_id]

    def _subscription_url(self, sub_id: str | None) -> str | None:
        base = str(self.options.get("sub_base_url") or "").rstrip("/")
        if not base or not sub_id:
            return None
        return f"{base}/{sub_id}"

    @staticmethod
    def _client_key(protocol: str, client: dict[str, Any]) -> str:
        return str(client["password"] if protocol == "trojan" else client["id"])

    def _build_client(self, protocol: str, email: str, data_limit: int, expire_at: datetime | None) -> dict:
        client: dict[str, Any] = {
            "email": email,
            "enable": True,
            "limitIp": int(self.options.get("limit_ip") or 0),
            "totalGB": int(data_limit),
            "expiryTime": int(expire_at.timestamp() * 1000) if expire_at else 0,
            "tgId": "",
            "subId": secrets.token_hex(8),
            "reset": 0,
            "comment": "",
        }
        if protocol == "trojan":
            client["password"] = secrets.token_urlsafe(12)
        else:
            client["id"] = str(uuid.uuid4())
        if protocol == "vless" and self.options.get("flow"):
            client["flow"] = self.options["flow"]
        if protocol == "vmess":
            client["security"] = "auto"
        return client

    def _panel_user(self, data: dict[str, Any], traffic: dict[str, Any] | None) -> PanelUser:
        client = data["client"]
        used = 0
        enabled = bool(client.get("enable", True))
        total = int(client.get("totalGB") or 0)
        expiry = client.get("expiryTime")
        if traffic:
            used = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
            enabled = bool(traffic.get("enable", enabled))
            total = int(traffic.get("total") or total)
            expiry = traffic.get("expiryTime", expiry)
        expire_at = parse_expire(expiry) if expiry and int(expiry) > 0 else None
        status = "active" if enabled else "disabled"
        if total and used >= total:
            status = "limited"
        elif expire_at and expire_at <= datetime.now(expire_at.tzinfo):
            status = "expired"
        return PanelUser(
            username=client["email"],
            status=status,
            used_bytes=used,
            data_limit=total,
            expire_at=expire_at,
            subscription_url=self._subscription_url(client.get("subId")),
            data=data,
        )

    async def test(self) -> str:
        inbounds = await self.request("GET", "/panel/api/inbounds/list")
        return f"inbounds={len(inbounds or [])}"

    async def list_options(self) -> list[PanelOption]:
        inbounds = await self.request("GET", "/panel/api/inbounds/list")
        return [
            PanelOption(
                id=str(inbound["id"]),
                title=f"{inbound.get('remark') or inbound['id']} · {inbound.get('protocol')}:{inbound.get('port')}",
            )
            for inbound in inbounds or []
        ]

    async def create_user(self, spec: UserSpec) -> PanelUser:
        inbound_id = self.inbound_id
        protocol = await self._protocol(inbound_id)
        client = self._build_client(protocol, spec.username, spec.data_limit, spec.expire_at)
        if spec.note:
            client["comment"] = spec.note[:120]
        await self.request(
            "POST",
            "/panel/api/inbounds/addClient",
            data={"id": str(inbound_id), "settings": json.dumps({"clients": [client]})},
        )
        data = {"inbound_id": inbound_id, "protocol": protocol, "client": client}
        return self._panel_user(data, None)

    async def _traffic(self, email: str) -> dict[str, Any] | None:
        return await self.request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        if "client" not in ref.data:
            return None
        traffic = await self._traffic(ref.username)
        if traffic is None:
            return None
        return self._panel_user(ref.data, traffic)

    async def _update(self, ref: PanelRef, client: dict[str, Any], old_key: str | None = None) -> None:
        protocol = ref.data["protocol"]
        key = old_key or self._client_key(protocol, client)
        await self.request(
            "POST",
            f"/panel/api/inbounds/updateClient/{key}",
            data={"id": str(ref.data["inbound_id"]), "settings": json.dumps({"clients": [client]})},
        )

    async def modify_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        client = dict(ref.data["client"])
        if expire_at is not UNSET:
            client["expiryTime"] = int(expire_at.timestamp() * 1000) if expire_at else 0
        if data_limit is not None:
            client["totalGB"] = int(data_limit)
        if enabled is not None:
            client["enable"] = bool(enabled)
        await self._update(ref, client)
        data = {**ref.data, "client": client}
        traffic = await self._traffic(ref.username)
        return self._panel_user(data, traffic)

    async def reset_usage(self, ref: PanelRef) -> None:
        await self.request(
            "POST", f"/panel/api/inbounds/{ref.data['inbound_id']}/resetClientTraffic/{ref.username}"
        )

    async def delete_user(self, ref: PanelRef) -> None:
        if "client" not in ref.data:
            return
        key = self._client_key(ref.data["protocol"], ref.data["client"])
        try:
            await self.request("POST", f"/panel/api/inbounds/{ref.data['inbound_id']}/delClient/{key}")
        except PanelError as exc:
            if "not found" not in str(exc).lower():
                raise

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        protocol = ref.data["protocol"]
        old_key = self._client_key(protocol, ref.data["client"])
        client = dict(ref.data["client"])
        client["subId"] = secrets.token_hex(8)
        if protocol == "trojan":
            client["password"] = secrets.token_urlsafe(12)
        else:
            client["id"] = str(uuid.uuid4())
        await self._update(ref, client, old_key=old_key)
        data = {**ref.data, "client": client}
        return self._panel_user(data, await self._traffic(ref.username))
