"""REF-07: staff find a service, and its owner, from the subscription link a customer sends."""

from __future__ import annotations

import httpx
import pytest
from aiogram.methods import SendMessage

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.bot.states import AdminStates
from paurubot.core import sub_links
from paurubot.db.models import Service, StaffRole
from tests import factories
from tests.bot import test_admin_customer
from tests.bot.test_admin_users import OWNER
from tests.fakes.telegram import message_update

env = test_admin_customer.env
TOKEN = "Zx9" + "a" * 29 + "Q1"  # 34 characters, like lifecycle.new_sub_token()
PANEL = "uk3HdT5pQ2mW8xYz"


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        (f"https://shop.example.com/s/{TOKEN}", TOKEN),
        (f"https://panel.example.com:8443/sub/{PANEL}", PANEL),
        (f"https://panel.example.com/sub/{PANEL}/?format=clash", PANEL),
        (TOKEN, TOKEN),  # a bare token long enough to never be a username
        ("someusername", None),
        ("short_token_1", None),
        ("javascript://x/abc", None),
        ("/sub/uk3HdT5pQ2mW8xYz", None),
        ("two words", None),
        ("", None),
    ],
)
def test_the_token_is_read_from_a_link(raw: str, token: str | None) -> None:
    assert sub_links.link_token(raw) == token


async def _service(session, shop, name: str, **kw) -> Service:  # type: ignore[no-untyped-def]
    panel = await factories.panel(session, shop)
    owner = await factories.customer(session, shop, username=f"owner_{name}")
    service = Service(shop_id=shop.id, customer_id=owner.id, panel_id=panel.id, panel_username=name, **kw)
    session.add(service)
    await session.flush()
    return service


async def test_branded_and_panel_links_find_the_service_in_its_shop_only(session) -> None:  # type: ignore[no-untyped-def]
    shop, other = await factories.shop(session), await factories.shop(session, "Other")
    branded = await _service(session, shop, "u_branded", sub_token=TOKEN)
    panel = await _service(
        session, shop, "u_panel", subscription_url=f"https://panel.example.com/sub/{PANEL}"
    )
    foreign = await _service(session, other, "u_foreign", sub_token="F" * 34)
    await session.commit()
    assert await sub_links.find(session, shop.id, f"https://shop.example.com/s/{TOKEN}") == branded
    assert await sub_links.find(session, shop.id, f"https://panel.example.com/sub/{PANEL}?x=1") == panel
    assert await sub_links.find(session, shop.id, f"https://shop.example.com/s/{'F' * 34}") is None
    assert await sub_links.find(session, other.id, "F" * 34) == foreign
    # A token that merely ends the same way is not a match.
    assert await sub_links.find(session, shop.id, f"https://panel.example.com/sub/x{PANEL}") is None


async def test_web_service_search_accepts_a_pasted_link(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    service = await _service(
        session, shop, "u_web", subscription_url=f"https://panel.example.com/sub/{PANEL}"
    )
    await _service(session, shop, "u_other")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="links_owner", password="a-very-strong-pass"
    )
    await session.commit()
    transport = httpx.ASGITransport(app=create_app(), client=("10.3.3.3", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login", json={"username": "links_owner", "password": "a-very-strong-pass"}
        )
        assert login.status_code == 200
        link = f"https://panel.example.com/sub/{PANEL}/" + "?format=" + "x" * 80  # longer than 64 characters
        found = await client.get("/api/admin/services", params={"q": link})
    assert found.status_code == 200, found.text
    assert [row["id"] for row in found.json()["items"]] == [service.id]


async def test_bot_lookup_shows_the_owner_of_a_link(session, env) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env["shop"], "u_bot", sub_token=TOKEN)
    await session.commit()
    state = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    await state.set_state(AdminStates.lookup)
    await env["send"](message_update(OWNER, f"https://shop.example.com/s/{TOKEN}"))
    texts = [call.text for call in env["fake"].calls if isinstance(call, SendMessage)]
    assert any("u_bot" in text for text in texts)  # the service card
    assert any("owner_u_bot" in text for text in texts)  # and its owner's card
    assert service.id
