from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.core import orders
from paurubot.db.models import Customer, OrderKind, PanelKind, Service, ServiceStatus
from paurubot.ledger import wallet
from paurubot.panels import GB, PanelUser, registry
from paurubot.provisioning import importer
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, FakeXuiPanel, transport


@pytest.fixture(autouse=True)
def clean_registry():  # type: ignore[no-untyped-def]
    yield
    registry.test_transports.clear()
    registry._cache.clear()


def _panel_user(fake: FakeTokenPanel, name: str, *, note: str = "", used: int = 0, limit: int = 0) -> None:
    fake.users[name] = {
        "id": len(fake.users) + 1,
        "username": name,
        "status": "active",
        "used_traffic": used,
        "data_limit": limit,
        "expire": 0,
        "sub": name + "sub",
        "note": note,
        "request": {},
    }


def test_telegram_id_detection() -> None:
    assert importer.telegram_id_of(PanelUser("x", note="tg:123456789")) == 123456789
    assert importer.telegram_id_of(PanelUser("x", note="Telegram ID 98765432")) == 98765432
    assert importer.telegram_id_of(PanelUser("x", note="  5550001112 ")) == 5550001112
    assert importer.telegram_id_of(PanelUser("vip123456789_a1b2")) == 123456789
    assert importer.telegram_id_of(PanelUser("user_55555555")) == 55555555
    assert importer.telegram_id_of(PanelUser("alice")) is None
    assert importer.telegram_id_of(PanelUser("x", note="call me at 12")) is None


async def test_import_matches_telegram_ids_and_falls_back(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel)
    buyer = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, buyer.id, 500_000, provider="card", payment_id=buyer.id)
    order = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    claimed = await queue.claim(session, "provision", limit=5)
    for job in claimed:
        await run_job("provision", QUEUES["provision"], job)
    reseller = await factories.customer(session, shop, username="reseller")
    _panel_user(fake, "old_one", note="tg:123456789", used=3 * GB, limit=10 * GB)
    _panel_user(fake, "user_55555555", used=GB, limit=GB)
    _panel_user(fake, "alice")
    await session.commit()

    preview = await importer.untracked(session, panel)
    assert sorted(u.username for u in preview) == ["alice", "old_one", "user_55555555"]

    result = await importer.run_import(
        session, panel, usernames=None, customer_id=reseller.id, match_telegram=True
    )
    assert (result.imported, result.skipped_existing, result.customers_created) == (3, 1, 2)
    rows = {
        s.panel_username: s
        for s in await session.scalars(select(Service).where(Service.panel_id == panel.id))
    }
    matched = await session.get(Customer, rows["old_one"].customer_id)
    assert matched is not None and matched.telegram_id == 123456789
    assert rows["old_one"].used_bytes == 3 * GB and rows["old_one"].sub_token
    assert rows["user_55555555"].status == ServiceStatus.LIMITED
    assert rows["alice"].customer_id == reseller.id

    again = await importer.run_import(session, panel, usernames=None, customer_id=None, match_telegram=True)
    assert again.imported == 0 and again.skipped_existing == 4


async def test_xui_users_are_listed_from_inbounds(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(
        session, shop, PanelKind.XUI, inbound_id=3, sub_base_url="https://sub.example.com/sub/"
    )
    fake = FakeXuiPanel()
    registry.test_transports[panel.id] = transport(fake)
    fake.clients["uuid-1"] = {
        "id": "uuid-1",
        "email": "legacy",
        "enable": True,
        "totalGB": 5 * GB,
        "expiryTime": 0,
        "tgId": "777888999",
        "subId": "abc",
    }
    fake.traffic["legacy"] = {"up": GB, "down": GB}
    reseller = await factories.customer(session, shop)
    await session.commit()
    result = await importer.run_import(
        session, panel, usernames=["legacy"], customer_id=reseller.id, match_telegram=True
    )
    assert result.imported == 1 and result.customers_created == 1
    service = await session.scalar(select(Service).where(Service.panel_id == panel.id))
    assert service is not None and service.used_bytes == 2 * GB and service.data_limit == 5 * GB
    assert service.panel_data["inbound_id"] == 3
