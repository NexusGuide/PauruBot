"""A PasarGuard panel whose bot admin may not read groups (a limited role) still tests and sells."""

from __future__ import annotations

import httpx

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import PanelKind, StaffRole
from paurubot.panels import UserSpec, driver_for, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport

PASSWORD = "a-very-strong-pass"


async def test_limited_admin_panel_test_and_typed_group_ids(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="pg_owner", password=PASSWORD)
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    await session.commit()
    fake = FakeTokenPanel("pasarguard")
    fake.group_perms = set()
    registry.test_transports[panel.id] = transport(fake)
    app = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    try:
        async with httpx.AsyncClient(transport=app, base_url="http://testserver") as client:
            login = await client.post("/api/auth/login", json={"username": "pg_owner", "password": PASSWORD})
            csrf = {"x-csrf-token": login.json()["csrf_token"]}
            result = (await client.post(f"/api/admin/panels/{panel.id}/test", headers=csrf)).json()
            assert result["ok"] and result["options"] == []
            assert "groups.read" in result["options_error"]
            saved = await client.put(
                f"/api/admin/panels/{panel.id}",
                json={
                    "name": panel.name,
                    "kind": "pasarguard",
                    "base_url": panel.base_url,
                    "username": "admin",
                    "options": {"group_ids": [7, 9]},
                },
                headers=csrf,
            )
            assert saved.status_code == 200, saved.text
        await session.refresh(panel)
        driver = await driver_for(panel)
        await driver.create_user(UserSpec(username="pg_typed_ids"))
        assert fake.users["pg_typed_ids"]["request"]["group_ids"] == [7, 9]
    finally:
        registry.test_transports.clear()
        registry._cache.clear()
