"""Moving panel users from one panel admin to another (TEN-11)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import update

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.db.models import Job, JobStatus, PanelKind, StaffRole
from paurubot.panels import PanelRef, driver_for, registry
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_service_lifecycle import drain

PASSWORD = "a-very-strong-pass"


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield
    registry.test_transports.clear()
    registry._cache.clear()


@pytest.mark.parametrize("kind", [PanelKind.MARZBAN, PanelKind.PASARGUARD])
async def test_owner_is_read_and_changed(session, kind) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, kind)
    fake = FakeTokenPanel(kind.value)
    fake.admins = {"ali"}
    fake.users["u1"] = {
        "id": 1,
        "username": "u1",
        "used_traffic": 0,
        "data_limit": 0,
        "expire": 0,
        "sub": "s1",
    }
    registry.test_transports[panel.id] = transport(fake)
    driver = await driver_for(panel)
    assert await driver.list_admins() == ["admin", "ali"]
    assert (await driver.get_user(PanelRef("u1", {}))).owner == "admin"  # type: ignore[union-attr]
    await driver.set_owner(PanelRef("u1", {}), "ali")
    assert fake.users["u1"]["admin"] == "ali"


async def test_transfer_moves_every_user_of_one_admin(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    fake.admins = {"ali", "sara"}
    for n, owner in enumerate(["ali", "ali", "sara"], start=1):
        fake.users[f"u{n}"] = {
            "id": n,
            "username": f"u{n}",
            "used_traffic": 0,
            "data_limit": 0,
            "expire": 0,
            "sub": f"s{n}",
            "admin": owner,
        }
    registry.test_transports[panel.id] = transport(fake)
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="mover2", password=PASSWORD)
    await session.commit()
    app = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=app, base_url="http://testserver") as client:
        login = await client.post("/api/auth/login", json={"username": "mover2", "password": PASSWORD})
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        listed = (await client.get(f"/api/admin/panels/{panel.id}/admins")).json()
        assert {a["username"]: a["users"] for a in listed["admins"]} == {"admin": 0, "ali": 2, "sara": 1}
        bad = await client.post(
            f"/api/admin/panels/{panel.id}/transfer",
            json={"from_admin": "ali", "to_admin": "nobody"},
            headers=csrf,
        )
        assert bad.status_code == 422
        done = await client.post(
            f"/api/admin/panels/{panel.id}/transfer",
            json={"from_admin": "ali", "to_admin": "sara"},
            headers=csrf,
        )
        assert done.json() == {"queued": 2}
    await drain(session, "bulk")
    assert {u["admin"] for u in fake.users.values()} == {"sara"}
