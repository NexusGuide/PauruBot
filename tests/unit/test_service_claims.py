"""Add a previous service: a customer brings a service they already have on one of the shop's
panels; it joins at once when the panel note names them, otherwise staff decide."""

from __future__ import annotations

import pytest

from paurubot.core import service_claims
from paurubot.core import settings as shop_settings
from paurubot.db.models import PanelKind
from paurubot.panels import registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_importer import _panel_user


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    other_shop = await factories.shop(session, "Other")
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    foreign = await factories.panel(session, other_shop, PanelKind.MARZBAN)
    fake, foreign_fake = FakeTokenPanel("marzban"), FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    registry.test_transports[foreign.id] = transport(foreign_fake)
    customer = await factories.customer(session, shop, telegram_id=55501234)
    stranger = await factories.customer(session, shop, telegram_id=55509999)
    await shop_settings.set_many(session, shop.id, {"service_import_enabled": True})
    await session.commit()
    yield {
        "shop": shop,
        "panel": panel,
        "fake": fake,
        "foreign_fake": foreign_fake,
        "customer": customer,
        "stranger": stranger,
    }
    registry.test_transports.clear()
    registry._cache.clear()


def test_a_link_or_a_name_gives_the_usernames_to_try() -> None:
    assert service_claims.candidates("@ali_1") == ["ali_1"]
    assert service_claims.candidates("https://sub.example.com/sub/ali_vpn/info") == ["info", "ali_vpn"]
    assert service_claims.candidates("bad name!") == []


async def test_the_note_names_the_customer_it_joins_at_once(session, env) -> None:  # type: ignore[no-untyped-def]
    _panel_user(env["fake"], "ali_vpn", note="tg:55501234", limit=10 * 1024**3)
    result = await service_claims.claim(session, env["customer"], "ali_vpn")
    assert (
        result.service is not None and result.claim.status == "approved" and result.claim.decided_by == "auto"
    )
    assert result.service.customer_id == env["customer"].id and result.service.data_limit == 10 * 1024**3
    with pytest.raises(service_claims.ClaimError) as again:
        await service_claims.claim(session, env["customer"], "ali_vpn")
    assert again.value.key == "import_already_yours"
    with pytest.raises(service_claims.ClaimError) as theirs:
        await service_claims.claim(session, env["stranger"], "ali_vpn")
    assert theirs.value.key == "import_taken"  # never handed over to someone else


async def test_without_proof_staff_decide(session, env) -> None:  # type: ignore[no-untyped-def]
    _panel_user(env["fake"], "old_user")
    result = await service_claims.claim(session, env["customer"], "https://x.example/sub/old_user")
    assert result.service is None and result.claim.status == "pending"
    with pytest.raises(service_claims.ClaimError) as twice:
        await service_claims.claim(session, env["customer"], "old_user")
    assert twice.value.key == "import_pending"
    service = await service_claims.decide(session, result.claim, True, "staff:1")
    assert (
        service is not None
        and service.customer_id == env["customer"].id
        and result.claim.status == "approved"
    )
    with pytest.raises(service_claims.ClaimError):
        await service_claims.decide(session, result.claim, False, "staff:1")


async def test_only_this_shops_panels_and_only_when_on(session, env) -> None:  # type: ignore[no-untyped-def]
    _panel_user(env["foreign_fake"], "elsewhere", note="tg:55501234")
    with pytest.raises(service_claims.ClaimError) as missing:
        await service_claims.claim(session, env["customer"], "elsewhere")
    assert missing.value.key == "import_not_found"
    await shop_settings.set_many(session, env["shop"].id, {"service_import_enabled": False})
    with pytest.raises(service_claims.ClaimError) as off:
        await service_claims.claim(session, env["customer"], "anything")
    assert off.value.key == "import_off"


async def test_auto_can_be_turned_off(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"service_import_auto": False})
    _panel_user(env["fake"], "noted", note="tg:55501234")
    result = await service_claims.claim(session, env["customer"], "noted")
    assert result.service is None and result.claim.status == "pending"
    assert await service_claims.decide(session, result.claim, False, "staff:1") is None
    assert result.claim.status == "rejected"
    again = await service_claims.claim(session, env["customer"], "noted")  # may ask once more
    assert again.claim.id == result.claim.id and again.claim.status == "pending"


async def test_only_the_panels_the_shop_allows(session, env) -> None:  # type: ignore[no-untyped-def]
    _panel_user(env["fake"], "allowed_only", note="tg:55501234")
    await shop_settings.set_many(
        session, env["shop"].id, {"service_import_panel_ids": [env["panel"].id + 999]}
    )
    with pytest.raises(service_claims.ClaimError) as missing:
        await service_claims.claim(session, env["customer"], "allowed_only")
    assert missing.value.key == "import_not_found"
    await shop_settings.set_many(session, env["shop"].id, {"service_import_panel_ids": [env["panel"].id]})
    assert (await service_claims.claim(session, env["customer"], "allowed_only")).service is not None
