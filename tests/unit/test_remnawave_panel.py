"""Adding a Remnawave panel with only an API token and selling on it (PNL-09)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import Panel, StaffRole
from paurubot.panels import UserSpec, driver_for, registry
from tests import factories
from tests.fakes.panels import FakeRemnawavePanel, transport

PASSWORD = "a-very-strong-pass"


async def test_token_only_panel(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="rw_owner", password=PASSWORD)
    await session.commit()
    app = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=app, base_url="http://testserver") as client:
        login = await client.post("/api/auth/login", json={"username": "rw_owner", "password": PASSWORD})
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        body = {
            "name": "RW",
            "kind": "remnawave",
            "base_url": "https://rw.example.com",
            "api_token": "tok-123",
        }
        made = await client.post(
            "/api/admin/panels", json={**body, "options": {"squad_ids": ["sq-1"]}}, headers=csrf
        )
        assert made.status_code == 200, made.text
        assert made.json()["has_api_token"] and made.json()["options"]["squad_ids"] == ["sq-1"]
        marzban = await client.post(
            "/api/admin/panels", json={**body, "kind": "marzban", "name": "MZ"}, headers=csrf
        )
        assert marzban.status_code == 422  # other panels still need a username and password
    panel = await session.scalar(select(Panel).where(Panel.shop_id == shop.id, Panel.name == "RW"))
    assert panel is not None
    fake = FakeRemnawavePanel(api_token="tok-123")
    registry.test_transports[panel.id] = transport(fake)
    try:
        driver = await driver_for(panel)
        user = await driver.create_user(UserSpec(username="rw_1", data_limit=1024**3))
        assert user.data["uuid"] and fake.logins == 0
        assert next(iter(fake.users.values()))["activeInternalSquads"][0]["uuid"] == "sq-1"
    finally:
        registry.test_transports.clear()
        registry._cache.clear()
