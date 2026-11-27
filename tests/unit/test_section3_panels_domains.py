"""Regression tests for Section 3 panel, webhook, monitor, importer, and custom-domain fixes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from paurubot.api.routes.admin.panels import RESTART_PATHS
from paurubot.api.routes.webhooks import _extract_username, _verify_remnawave_signature, panel_webhook
from paurubot.core import domains
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, PanelCheck, PanelKind, ShopDomain
from paurubot.panels import GB, PanelUser, UserSpec, registry
from paurubot.panels.drivers.pasarguard import PasarGuardDriver
from paurubot.panels.drivers.remnawave import RemnawaveDriver
from paurubot.panels.http import SafeHttpClient
from paurubot.provisioning import importer, monitor
from paurubot.security.secretbox import encrypt
from tests import factories


def test_pasarguard_sub_formats_and_xui_restart_path() -> None:
    assert registry.formats_of("pasarguard") == ["v2ray", "clash", "clash-meta", "sing-box", "xray-json"]
    drv = PasarGuardDriver.__new__(PasarGuardDriver)
    assert drv.format_url("https://panel.example.com/sub/abc", "clash-meta") == (
        "https://panel.example.com/sub/abc/clash-meta"
    )
    assert RESTART_PATHS[PanelKind.XUI] == ("POST", "/panel/api/server/restartXrayService")
    assert RemnawaveDriver.supports_webhooks is True


async def test_pasarguard_template_create_preserves_unset_limits() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        is_json = "json" in request.headers.get("content-type", "")
        body = json.loads(request.content) if request.content and is_json else {}
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/api/user/from_template":
            return httpx.Response(
                200,
                json={
                    "username": body["username"],
                    "status": "active",
                    "data_limit": 20 * GB,
                    "used_traffic": 0,
                    "expire": "2030-01-01T00:00:00Z",
                },
            )
        if request.method == "PUT" and request.url.path.startswith("/api/user/"):
            return httpx.Response(
                200,
                json={
                    "username": "u1",
                    "status": "active",
                    "data_limit": body.get("data_limit", 20 * GB),
                    "used_traffic": 0,
                    "expire": body.get("expire", "2030-01-01T00:00:00Z"),
                },
            )
        return httpx.Response(404)

    http = SafeHttpClient("https://pg.example.com", transport=httpx.MockTransport(handler))
    driver = PasarGuardDriver(http, "admin", "secret", {"template_id": 5})
    try:
        # Only expire_at given: must NOT send data_limit=0 (which would wipe template limit).
        exp = datetime(2031, 5, 1, tzinfo=UTC)
        await driver.create_user(UserSpec(username="u1", data_limit=0, expire_at=exp, ip_limit=2))
        put_payload = next(b for m, _, b in calls if m == "PUT")
        assert "data_limit" not in put_payload
        assert put_payload["expire"] == "2031-05-01T00:00:00Z"
        assert put_payload["hwid_limit"] == 2

        # Only data_limit given: must NOT send expire=0 (which would wipe template expiry).
        calls.clear()
        await driver.create_user(UserSpec(username="u2", data_limit=50 * GB, expire_at=None))
        put_payload = next(b for m, _, b in calls if m == "PUT")
        assert put_payload == {"data_limit": 50 * GB}

        # on_hold_days given: must apply on_hold lifecycle fields.
        calls.clear()
        await driver.create_user(UserSpec(username="u3", data_limit=0, expire_at=None, on_hold_days=30))
        put_payload = next(b for m, _, b in calls if m == "PUT")
        assert put_payload["status"] == "on_hold"
        assert put_payload["on_hold_expire_duration"] == 30 * 86400
    finally:
        await driver.close()


async def test_fetch_subscription_strips_subpath_and_reuses_pinned_cert(monkeypatch) -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/secret/sub/abc":
            return httpx.Response(200, content=b"vless://ok", headers={"content-type": "text/plain"})
        return httpx.Response(404)

    http = SafeHttpClient(
        "https://panel.example.com/secret",
        pinned_pem="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----",
        transport=httpx.MockTransport(handler),
    )
    driver = PasarGuardDriver(http, "a", "b")
    try:
        body = await driver.fetch_subscription("https://panel.example.com/secret/sub/abc")
        assert body is not None and body.content == b"vless://ok"
        assert seen_paths == ["/secret/sub/abc"]  # never doubled to /secret/secret/sub/abc
    finally:
        await driver.close()

    # Different path on same origin: must pass pinned_pem to the temporary SafeHttpClient.
    captured_pins: list[str | None] = []

    class DummyClient:
        def __init__(self, origin: str, *, pinned_pem: str | None = None, allow_private: bool = False):
            captured_pins.append(pinned_pem)

        async def request(self, method: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("paurubot.panels.http.SafeHttpClient", DummyClient)
    pem = "-----BEGIN CERTIFICATE-----\nPIN\n-----END CERTIFICATE-----"
    http2 = SafeHttpClient(
        "https://panel.example.com/admin-path",
        pinned_pem=pem,
        transport=httpx.MockTransport(lambda req: httpx.Response(404)),
    )
    driver2 = PasarGuardDriver(http2, "a", "b")
    try:
        await driver2.fetch_subscription("https://panel.example.com/client-path/uuid/")
        await driver2.fetch_subscription("https://other.example.com/sub/uuid/")
        assert captured_pins == [pem, None]
    finally:
        await driver2.close()


async def test_importer_all_users_paginates_when_driver_caps_page_size(monkeypatch) -> None:
    class CappedDriver:
        def __init__(self, total: int, cap: int = 100) -> None:
            self._all = [PanelUser(username=f"u{i}") for i in range(total)]
            self._cap = cap

        async def list_users(self, offset: int = 0, limit: int = 100):  # type: ignore[no-untyped-def]
            size = min(limit, self._cap)
            return self._all[offset : offset + size], len(self._all)

    fake_driver = CappedDriver(total=250, cap=100)
    monkeypatch.setattr(importer, "driver_for", AsyncMock(return_value=fake_driver))
    panel = SimpleNamespace(id=1)
    users = await importer.all_users(panel)  # type: ignore[arg-type]
    assert len(users) == 250
    assert users[0].username == "u0" and users[-1].username == "u249"


async def test_monitor_skips_national_mode_panels(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    panel.national_mode = True
    panel.is_healthy = True
    await session.commit()

    res = await monitor.check_panel(session, panel)
    assert res.error == "national_network_mode" and res.events == []
    assert panel.is_healthy is True and panel.down_since is None
    checks = list(await session.scalars(select(PanelCheck).where(PanelCheck.panel_id == panel.id)))
    assert checks == []


def test_remnawave_webhook_signature_and_extraction() -> None:
    secret = "whsec_test_123"
    raw = json.dumps({"event": "user.expired", "data": {"username": "alice_rw"}}).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    assert _verify_remnawave_signature(raw, secret, sig)
    assert _verify_remnawave_signature(raw, secret, f"sha256={sig}")
    assert not _verify_remnawave_signature(raw, secret, "bad-sig")
    assert not _verify_remnawave_signature(raw, secret, None)
    assert _extract_username(json.loads(raw), PanelKind.REMNAWAVE) == "alice_rw"
    assert _extract_username({"user": {"username": "bob_mz"}}, PanelKind.MARZBAN) == "bob_mz"


async def test_webhook_skips_national_mode_cleanly(monkeypatch) -> None:
    secret = "my-secret"
    panel = SimpleNamespace(
        id=9,
        kind=PanelKind.PASARGUARD,
        national_mode=True,
        webhook_secret_enc=encrypt("webhook", secret),
    )
    db = AsyncMock()
    db.get.return_value = panel
    body = json.dumps([{"user": {"username": "alice"}}]).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    from starlette.requests import Request

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/webhooks/panel/9",
            "headers": [(b"x-webhook-secret", secret.encode())],
        },
        receive,
    )
    result = await panel_webhook(9, req, db)
    assert result == {"received": 1, "applied": 0, "queued": 0}


async def test_domain_netprobe_reads_split_and_chunked_responses() -> None:
    mark = domains.marker("sub.example.com")

    class SplitReader:
        def __init__(self, frames: list[bytes]) -> None:
            self._frames = list(frames)

        async def read(self, n: int = -1) -> bytes:
            return self._frames.pop(0) if self._frames else b""

    class DummyWriter:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    probe = domains.NetProbe()
    # Frame 1: headers only; Frame 2: body (common over TLS / Caddy).
    split = [
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n",
        mark.encode(),
    ]
    probe._open = AsyncMock(return_value=(SplitReader(split), DummyWriter()))  # type: ignore[method-assign]
    assert await probe.fetch_mark("sub.example.com") == mark

    # Chunked transfer encoding split across frames.
    chunked = [
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        f"{len(mark):x}\r\n{mark}\r\n0\r\n\r\n".encode(),
    ]
    probe._open = AsyncMock(return_value=(SplitReader(chunked), DummyWriter()))  # type: ignore[method-assign]
    assert await probe.fetch_mark("sub.example.com") == mark


async def test_domain_run_checks_alerts_on_degradation_recovery_and_cert_expiry(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    row = ShopDomain(
        shop_id=shop.id,
        host="sub.example.com",
        status=domains.OK,
        for_links=True,
        cert_expires_at=utcnow() + timedelta(days=60),
    )
    session.add(row)
    await session.commit()

    class FixedProbe:
        def __init__(self, result: domains.CheckResult) -> None:
            self.result = result

        async def resolve(self, host: str) -> list[str]:
            return ["1.1.1.1"]

        async def certificate(self, host: str) -> domains.CertInfo:
            if self.result.status == domains.TLS:
                raise domains.ProbeError("expired")
            return domains.CertInfo(self.result.expires_at, self.result.issuer)

        async def fetch_mark(self, host: str) -> str:
            return domains.marker(host)

    # 1. Domain breaks (OK -> TLS): queues domain_alert(down)
    await domains.run_checks(session, probe=FixedProbe(domains.CheckResult(domains.TLS, "expired")))
    jobs = list(
        await session.scalars(
            select(Job).where(Job.kind == "domain_alert", Job.status == JobStatus.QUEUED).order_by(Job.id)
        )
    )
    assert [j.payload["event"] for j in jobs] == ["down"]

    # 2. Domain recovers with a certificate expiring in 5 days: queues up + cert_expiring
    soon = utcnow() + timedelta(days=5)
    await domains.run_checks(session, probe=FixedProbe(domains.CheckResult(domains.OK, None, soon, "LE")))
    jobs = list(
        await session.scalars(
            select(Job).where(Job.kind == "domain_alert", Job.status == JobStatus.QUEUED).order_by(Job.id)
        )
    )
    assert [j.payload["event"] for j in jobs] == ["down", "up", "cert_expiring"]
    assert jobs[-1].payload["days"] <= 5
    await asyncio.sleep(0)
