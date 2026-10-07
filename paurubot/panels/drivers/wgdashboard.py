"""WGDashboard: plain WireGuard peers on one configuration (wg0, …) (PNL-08).

Requests carry the dashboard's API key in `wg-dashboard-apikey`. A peer is addressed by its public
key; the bot makes the key pair itself and hands it to the dashboard. WGDashboard has no data or
time limits of its own: they are "schedule jobs" that restrict the peer once total data (GB) or a
date (the dashboard's local time) passes. The customer gets a .conf file instead of a link, so the
"subscription URL" kept for a peer is the dashboard's own download address for it, read with the
API key and never shown to customers.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from paurubot.config import get_settings
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
    SubscriptionBody,
    UserSpec,
    as_dict,
    number,
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def keypair() -> tuple[str, str]:
    """(private, public) WireGuard keys, base64 of the raw 32 bytes."""
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw_private).decode(), base64.b64encode(raw_public).decode()


def preshared() -> str:
    return base64.b64encode(os.urandom(32)).decode()


class WGDashboardDriver(PanelDriver):
    kind = "wgdashboard"
    supports_webhooks = False
    delivery = "wireguard"

    @property
    def config(self) -> str:
        name = str(self.options.get("configuration") or self.options.get("inbound_id") or "wg0")
        return name

    def _key(self) -> str:
        return str(self.options.get("api_token") or self.password or "")

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json", "wg-dashboard-apikey": self._key()}
        resp = await self.http.request(method, path, headers=headers, **kwargs)
        if resp.status_code in (401, 403):
            raise PanelError("API key rejected", resp.status_code)
        if resp.status_code >= 400:
            raise PanelError(f"{method} {path} failed", resp.status_code, resp.text[:300])
        try:
            body = resp.json()
        except ValueError as exc:
            raise PanelError("unexpected non-JSON response", resp.status_code) from exc
        if isinstance(body, dict) and body.get("status") is False:
            raise PanelError(str(body.get("message") or f"{method} {path} rejected"), resp.status_code)
        return body.get("data") if isinstance(body, dict) else body

    # --- peers and jobs -------------------------------------------------------

    async def _peers(self) -> list[tuple[dict[str, Any], bool]]:
        """Every peer with whether it is currently restricted."""
        info = as_dict(
            await self.call(
                "GET", "/api/getWireguardConfigurationInfo", params={"configurationName": self.config}
            )
        )
        rows: list[tuple[dict[str, Any], bool]] = []
        for key, restricted in (("configurationPeers", False), ("configurationRestrictedPeers", True)):
            for peer in info.get(key) or []:
                if isinstance(peer, dict):
                    rows.append((peer, restricted))
        return rows

    async def _peer(self, ref: PanelRef) -> tuple[dict[str, Any], bool]:
        public = ref.data.get("id")
        for peer, restricted in await self._peers():
            if (public and peer.get("id") == public) or (not public and peer.get("name") == ref.username):
                return peer, restricted
        raise PanelNotFound(f"peer {ref.username} not found", 404)

    @staticmethod
    def _job(peer: dict[str, Any], field: str) -> dict[str, Any] | None:
        for job in peer.get("jobs") or []:
            if isinstance(job, dict) and job.get("Field") == field and job.get("Action") == "restrict":
                return job
        return None

    async def _set_job(self, public: str, field: str, value: str) -> None:
        job = {
            "JobID": str(uuid.uuid4()),
            "Configuration": self.config,
            "Peer": public,
            "Field": field,
            "Operator": "lgt",
            "Value": value,
            "CreationDate": "",
            "ExpireDate": None,
            "Action": "restrict",
        }
        await self.call("POST", "/api/savePeerScheduleJob", json={"Job": job})

    async def _drop_job(self, job: dict[str, Any] | None) -> None:
        if job is not None:
            await self.call("POST", "/api/deletePeerScheduleJob", json={"Job": job})

    @staticmethod
    def _local(value: datetime) -> str:
        return value.astimezone(get_settings().tz).strftime(DATE_FORMAT)

    @staticmethod
    def _parse_local(value: Any) -> datetime | None:
        try:
            naive = datetime.strptime(str(value), DATE_FORMAT)
        except (TypeError, ValueError):
            return None
        return naive.replace(tzinfo=get_settings().tz).astimezone(UTC)

    async def _apply_limits(self, public: str, data_limit: int, expire_at: datetime | None) -> None:
        if data_limit:
            await self._set_job(public, "total_data", f"{data_limit / GB:.3f}".rstrip("0").rstrip("."))
        if expire_at is not None:
            await self._set_job(public, "date", self._local(expire_at))

    def download_url(self, public: str) -> str:
        return f"{self.base_url}/api/downloadPeer/{quote(self.config, safe='')}?id={quote(public, safe='')}"

    def parse_user(self, peer: dict[str, Any], restricted: bool) -> PanelUser:
        used = int(((number(peer, "total_data") or 0) + (number(peer, "cumu_data") or 0)) * GB)
        volume = self._job(peer, "total_data")
        limit = int(float(volume["Value"]) * GB) if volume and str(volume.get("Value", "")).strip() else 0
        date = self._job(peer, "date")
        expire = self._parse_local(date.get("Value")) if date else None
        if restricted:
            status = (
                "limited"
                if limit and used >= limit
                else "expired"
                if expire and expire <= datetime.now(UTC)
                else "disabled"
            )
        elif expire is not None and expire <= datetime.now(UTC):
            status = "expired"
        elif limit and used >= limit:
            status = "limited"
        else:
            status = "active"
        public = str(peer.get("id") or "")
        return PanelUser(
            username=str(peer.get("name") or ""),
            status=status,
            used_bytes=used,
            data_limit=limit,
            expire_at=expire,
            subscription_url=self.download_url(public) if public else None,
            data={"id": public} if public else {},
        )

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        peers = await self._peers()
        return PanelHealth(True, "ok", len(peers), None)

    async def list_options(self) -> list[PanelOption]:
        return []

    async def _free_ip(self) -> str:
        data = await self.call("GET", f"/api/getAvailableIPs/{quote(self.config, safe='')}")
        for value in (data or {}).values() if isinstance(data, dict) else data or []:
            for candidate in value if isinstance(value, list) else [value]:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        raise PanelError("the WireGuard configuration has no free address left")

    async def _add(self, name: str, data_limit: int, expire_at: datetime | None) -> str:
        private, public = keypair()
        await self.call(
            "POST",
            f"/api/addPeers/{quote(self.config, safe='')}",
            json={
                "name": name,
                "allowed_ips": [await self._free_ip()],
                "private_key": private,
                "public_key": public,
                "preshared_key": preshared(),
            },
        )
        await self._apply_limits(public, data_limit, expire_at)
        return public

    async def create_user(self, spec: UserSpec) -> PanelUser:
        public = await self._add(spec.username, int(spec.data_limit or 0), spec.expire_at)
        return self.parse_user(*await self._peer(PanelRef(spec.username, {"id": public})))

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            return self.parse_user(*await self._peer(ref))
        except PanelNotFound:
            return None

    async def _allow(self, public: str) -> None:
        await self.call(
            "POST", f"/api/allowAccessPeers/{quote(self.config, safe='')}", json={"peers": [public]}
        )

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        peer, restricted = await self._peer(ref)
        public = str(peer["id"])
        if data_limit is not None:
            await self._drop_job(self._job(peer, "total_data"))
            if data_limit:
                await self._set_job(public, "total_data", f"{data_limit / GB:.3f}".rstrip("0").rstrip("."))
        if expire_at is not UNSET:
            await self._drop_job(self._job(peer, "date"))
            if expire_at is not None:
                await self._set_job(public, "date", self._local(expire_at))
        if enabled is False:
            await self.call(
                "POST", f"/api/restrictPeers/{quote(self.config, safe='')}", json={"peers": [public]}
            )
        elif restricted and (enabled is True or data_limit is not None or expire_at is not UNSET):
            await self._allow(public)
        return self.parse_user(*await self._peer(PanelRef(ref.username, {"id": public})))

    async def reset_usage(self, ref: PanelRef) -> None:
        peer, restricted = await self._peer(ref)
        if restricted:
            await self._allow(str(peer["id"]))
        await self.call(
            "POST",
            f"/api/resetPeerData/{quote(self.config, safe='')}",
            json={"id": peer["id"], "type": "total"},
        )

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            peer, restricted = await self._peer(ref)
        except PanelNotFound:
            return
        if restricted:
            await self._allow(str(peer["id"]))
        await self.call(
            "POST", f"/api/deletePeers/{quote(self.config, safe='')}", json={"peers": [peer["id"]]}
        )

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        """A leaked WireGuard config can only be retired: same name and limits, new keys."""
        old = self.parse_user(*await self._peer(ref))
        public = await self._add(ref.username, old.data_limit, old.expire_at)
        await self.delete_user(PanelRef(ref.username, {"id": old.data.get("id")}))
        return self.parse_user(*await self._peer(PanelRef(ref.username, {"id": public})))

    async def list_users(self, offset: int = 0, limit: int = 100) -> tuple[list[PanelUser], int | None]:
        rows = await self._peers()
        page = rows[offset : offset + max(1, limit)]
        return [self.parse_user(peer, restricted) for peer, restricted in page if peer.get("name")], len(rows)

    # --- the .conf file -------------------------------------------------------

    async def config_text(self, subscription_url: str | None) -> str | None:
        if not subscription_url or not subscription_url.startswith(f"{self.base_url}/api/downloadPeer/"):
            return None
        parts = urlsplit(subscription_url)
        public = (parse_qs(parts.query).get("id") or [""])[0]
        data = await self.call(
            "GET", f"/api/downloadPeer/{quote(self.config, safe='')}", params={"id": public}
        )
        text = data.get("file") if isinstance(data, dict) else None
        return str(text) if text else None

    async def fetch_subscription(
        self, subscription_url: str | None, user_agent: str = "v2rayNG/1.8.0"
    ) -> SubscriptionBody | None:
        try:
            text = await self.config_text(subscription_url)
        except PanelError:
            return None
        if text is None:
            return None
        name = f"{self.config}.conf"
        return SubscriptionBody(
            text.encode(),
            "text/plain; charset=utf-8",
            {"content-disposition": f'attachment; filename="{name}"'},
        )

    async def fetch_links(self, subscription_url: str | None) -> list[str]:
        try:
            text = await self.config_text(subscription_url)
        except PanelError:
            return []
        return [text] if text else []
