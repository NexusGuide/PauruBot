"""Per-customer controls and conveniences: hidden panels (CAT-10), another trial (CAT-11), a
service name of the customer's choosing (CAT-12) and every link in one message (SVC-17)."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from sqlalchemy import select, update

from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin, Pay, Shop, Svc
from paurubot.core import catalog, orders, resale, trials, usernames
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Job,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    ResellerProfile,
    Service,
    StaffRole,
    TrialGrant,
)
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_provisioning import drain

FA = Texts("fa")
OWNER = 7901
UID = 7902
OTHER = 7903


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Controls")
    panel = await factories.panel(session, shop)
    fake_panel = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake_panel)
    plan = await factories.plan(session, shop, panel, price=100_000, title="Monthly")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="ctl_owner", password=None, telegram_id=OWNER
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake, "send": send, "fake_panel": fake_panel}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [text for text, _ in fake.buttons(fake.last(kind))]


def alerts(fake) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.text for c in fake.calls if isinstance(c, AnswerCallbackQuery) and c.text]


_payments = iter(range(790_000, 799_999))


async def _buy(session, env, customer, wanted: str | None = None) -> Service:  # type: ignore[no-untyped-def]
    await wallet.topup(
        session, env["shop"].id, customer.id, 100_000, provider="card", payment_id=next(_payments)
    )
    draft = await orders.draft(
        session, customer, OrderKind.PURCHASE, plan=env["plan"], wanted_username=wanted
    )
    order = await orders.create(session, customer, draft)
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    service = await session.get(Service, order.service_id)
    assert service is not None
    return service


# --- CAT-10 -----------------------------------------------------------------------------------


async def test_owner_hides_a_panel_from_one_customer(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    second = await factories.panel(session, shop, PanelKind.PASARGUARD)
    hidden_plan = await factories.plan(session, shop, second, price=50_000, title="Cheap")
    await session.commit()
    await send(message_update(UID, "/start"))
    await send(message_update(OTHER, "/start"))
    customer = await _customer(session, shop.id, UID)
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="user", id=customer.id).pack()))
    assert FA("adm_user_btn_hide") in labels(fake)
    await send(callback_update(OWNER, Admin(section="uhide", id=customer.id).pack()))
    assert len([text for text in labels(fake) if text.startswith("👁")]) == 2
    await send(
        callback_update(OWNER, Admin(section="uhide", action="toggle", id=customer.id, page=second.id).pack())
    )
    await session.refresh(customer)
    assert customer.hidden_panel_ids == [second.id]
    assert [p.id for p in await catalog.plans(session, shop.id, customer)] == [env["plan"].id]
    assert await catalog.get_plan(session, shop.id, hidden_plan.id, customer) is None
    other = await _customer(session, shop.id, OTHER)
    assert {p.id for p in await catalog.plans(session, shop.id, other)} == {env["plan"].id, hidden_plan.id}
    # A panel of another shop can't be slipped in.
    elsewhere = await factories.panel(session, await factories.shop(session, "Elsewhere"))
    await session.commit()
    await send(
        callback_update(
            OWNER, Admin(section="uhide", action="toggle", id=customer.id, page=elsewhere.id).pack()
        )
    )
    await session.refresh(customer)
    assert customer.hidden_panel_ids == [second.id]


async def test_a_resellers_hidden_panel_stays_hidden_in_its_bot(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "hidden_bot"

    monkeypatch.setattr(resale, "bot_identity", identity)
    shop = env["shop"]
    reseller = await factories.customer(session, shop, telegram_id=OTHER)
    profile = ResellerProfile(shop_id=shop.id, customer_id=reseller.id, permissions=["resellers.bot"])
    session.add(profile)
    await session.flush()
    child = await resale.connect(session, profile, "776655443:AAH-hidden-panel-test-token-xxxxxxxxx", "")
    await session.commit()
    assert [p.id for p in await catalog.plans(session, child.id, None)] == [env["plan"].id]
    reseller.hidden_panel_ids = [env["panel"].id]
    await session.commit()
    assert await catalog.plans(session, child.id, None) == []


# --- CAT-11 -----------------------------------------------------------------------------------


async def test_another_trial_for_one_and_for_everyone(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(UID, "/start"))
    customer = await _customer(session, shop.id, UID)
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="utrial", id=customer.id).pack()))
    assert FA("adm_trial_not_used") in alerts(fake)

    session.add(TrialGrant(shop_id=shop.id, customer_id=customer.id, generation=1, status="issued"))
    await session.commit()
    assert await trials.eligibility(session, customer) == "trial_already_used"
    await send(callback_update(OWNER, Admin(section="utrial", id=customer.id).pack()))
    assert FA("adm_trial_again_done") in alerts(fake)
    assert await trials.eligibility(session, customer) is None
    kept = list(await session.scalars(select(TrialGrant).where(TrialGrant.customer_id == customer.id)))
    assert len(kept) == 1 and kept[0].generation < 0  # still counted in reports

    session.add(TrialGrant(shop_id=shop.id, customer_id=customer.id, generation=1, status="issued"))
    await session.commit()
    assert await trials.eligibility(session, customer) == "trial_already_used"
    await send(callback_update(OWNER, Admin(section="settings", action="group", page=2).pack()))
    assert FA("adm_trial_reset_all") in labels(fake)
    await send(callback_update(OWNER, Admin(section="trialreset").pack()))
    assert FA("adm_trial_reset_yes") in labels(fake)
    await send(callback_update(OWNER, Admin(section="trialreset", action="go").pack()))
    shop_settings.invalidate(shop.id)
    assert (await shop_settings.load(session, shop.id, fresh=True))["trial_generation"] == 2
    assert await trials.eligibility(session, customer) is None


# --- CAT-12 -----------------------------------------------------------------------------------


def test_wanted_names() -> None:
    assert usernames.wanted(" Ali_VPN ") == "ali_vpn"
    for bad in ("ab", "1abc", "a__b", "ali vpn", "x" * 17, "\u0639\u0644\u06cc", None):
        assert usernames.wanted(bad) is None, bad


async def test_customer_names_its_service(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await shop_settings.set_many(session, shop.id, {"custom_username_enabled": True})
    await session.commit()
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, Shop(step="plan", plan=env["plan"].id).pack()))
    assert FA("ask_username") in fake.texts(UID)
    await send(message_update(UID, "Bad name!"))
    assert FA("username_invalid") in fake.texts(UID)
    await send(message_update(UID, "ali_vpn"))
    order = await session.scalar(select(Order).order_by(Order.id.desc()))
    assert order is not None and order.params["wanted_username"] == "ali_vpn"
    customer = await _customer(session, shop.id, UID)
    await wallet.topup(session, shop.id, customer.id, 100_000, provider="card", payment_id=79_021)
    await session.commit()
    await send(callback_update(UID, Pay(method="wallet", order=order.id).pack()))
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None and service.panel_username == "ali_vpn"

    # Someone else asking for the same name never gets that account: a tail is added.
    other = await factories.customer(session, shop, telegram_id=OTHER)
    second = await _buy(session, env, other, "ali_vpn")
    assert second.panel_username.startswith("ali_vpn_") and second.panel_username != "ali_vpn"
    # So does a name that only exists on the panel itself.
    env["fake_panel"].users["outside"] = {**env["fake_panel"].users["ali_vpn"], "username": "outside"}
    third = await _buy(session, env, await factories.customer(session, shop), "outside")
    assert third.panel_username.startswith("outside_")


async def test_skipping_the_name_keeps_the_usual_one(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await shop_settings.set_many(session, shop.id, {"custom_username_enabled": True})
    await session.commit()
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, Shop(step="plan", plan=env["plan"].id).pack()))
    await send(message_update(UID, "-"))
    assert FA("username_skipped") in fake.texts(UID)
    order = await session.scalar(select(Order).order_by(Order.id.desc()))
    assert order is not None and "wanted_username" not in order.params


# --- SVC-17 -----------------------------------------------------------------------------------


async def test_all_links_in_one_message_and_configs_in_one_file(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(UID, "/start"))
    customer = await _customer(session, shop.id, UID)
    first = await _buy(session, env, customer)
    second = await _buy(session, env, customer)
    await send(callback_update(UID, Svc(action="list").pack()))
    assert FA("btn_all_links") in labels(fake)
    fake.reset()
    await send(callback_update(UID, Svc(action="all").pack()))
    sent = fake.last(SendMessage)
    assert first.panel_username in sent.text and second.panel_username in sent.text
    assert FA("btn_all_configs_file") in [text for text, _ in fake.buttons(sent)]
    await send(callback_update(UID, Svc(action="all_file").pack()))
    document = fake.last(SendDocument)
    body = document.document.data.decode()
    assert f"vless://{first.panel_username}@" in body and f"vless://{second.panel_username}@" in body
    # Someone without services gets told so.
    await send(message_update(OTHER, "/start"))
    await send(callback_update(OTHER, Svc(action="all").pack()))
    assert FA("no_services") in alerts(fake)
