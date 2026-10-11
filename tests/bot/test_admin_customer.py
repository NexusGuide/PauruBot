"""Table-style list, customer navigation and protected payment history."""

from __future__ import annotations

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from sqlalchemy import update

from paurubot.auth import rbac
from paurubot.bot.callbacks import Admin, Nav
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers import admin, admin_hub
from paurubot.bot.utils import clear_customer_input
from paurubot.db.models import Customer, Payment, PaymentPurpose, PaymentStatus, StaffRole
from paurubot.i18n import Texts, catalog, fields
from tests import factories
from tests.bot.test_admin_users import OWNER, SUPPORT
from tests.fakes.telegram import callback_update

FA = Texts("fa")


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    from aiogram.fsm.storage.memory import MemoryStorage

    from paurubot.auth.staff import create_staff
    from paurubot.bot.app import build_dispatcher
    from tests.fakes.telegram import make_bot

    shop = await factories.shop(session, "Customer detail")
    for role, uid in ((StaffRole.OWNER, OWNER), (StaffRole.SUPPORT, SUPPORT)):
        await create_staff(
            session, shop.id, role=role, username=f"detail_{uid}", password=None, telegram_id=uid
        )
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "send": send, "fake": fake, "dp": dp, "bot": bot}
    await bot.session.close()


def test_detail_translations() -> None:
    fa, en = catalog("fa"), catalog("en")
    keys = [key for key in fa if key.startswith("adm_ud_")]
    assert keys
    for key in keys:
        assert key in en and fields(fa[key]) == fields(en[key])


async def test_three_column_list_and_manage_target(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"], username="test_table")
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="ulist", action="uf", id=0).pack()))
    result = env["fake"].last(EditMessageText)
    rows = result.reply_markup.inline_keyboard
    assert [b.text for b in rows[0]] == [FA("adm_ud_operations"), FA("adm_ud_username"), FA("adm_ud_id")]
    row = next(row for row in rows if any(b.text == str(cust.telegram_id) for b in row))
    assert len(row) == 3
    assert row[0].text == FA("adm_ud_manage") and row[1].text == "@test_table"
    target = Admin.unpack(row[0].callback_data)
    assert target.section == "user" and target.id == cust.id and target.action == "uf0" and target.page == 0
    assert row[1].callback_data == Nav(to="noop").pack()
    assert all(len(b.callback_data.encode()) <= 64 for row in rows for b in row if b.callback_data)


@pytest.mark.parametrize("action", ["uf", "f"])
async def test_filtered_page_survives_profile_and_children(session, env, action):  # type: ignore[no-untyped-def]
    customers = [await factories.customer(session, env["shop"], username=f"paged_{i}") for i in range(13)]
    await session.execute(
        update(Customer).where(Customer.id.in_([c.id for c in customers])).values(is_banned=True)
    )
    await session.commit()
    idx = admin_hub.USER_FILTERS.index("banned")
    await env["send"](callback_update(OWNER, Admin(section="ulist", action=action, id=idx, page=1).pack()))
    fake = env["fake"]
    target = next(
        data for label, data in fake.buttons(fake.last(EditMessageText)) if label == FA("adm_ud_manage")
    )
    selected = Admin.unpack(target)
    await env["send"](callback_update(OWNER, target))
    assert FA("adm_ud_back_list") in [label for label, _ in fake.buttons(fake.last(EditMessageText))]
    await env["send"](callback_update(OWNER, Admin(section="uref", id=selected.id).pack()))
    await env["send"](callback_update(OWNER, Admin(section="user", id=selected.id).pack()))
    await env["send"](callback_update(OWNER, Admin(section="uprofile", id=selected.id).pack()))
    result = fake.last(EditMessageText)
    assert FA("adm_ulist_head", title=FA("adm_ulist_banned"), total=13, page=2, pages=2) in result.text
    expected = Admin(section="users" if action == "uf" else "ulist").pack()
    assert expected in [v for _, v in fake.buttons(result)]


async def test_no_username_empty_list_clamp_and_close(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"], username=None)
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="ulist", action="uf", id=0, page=999).pack()))
    fake = env["fake"]
    assert FA("adm_ud_no_username") in [s for s, _ in fake.buttons(fake.last(EditMessageText))]
    await env["send"](callback_update(OWNER, Admin(section="uprofile", action="close").pack()))
    result = fake.last(EditMessageText)
    assert result.text == FA("adm_ud_closed") and result.reply_markup is None
    await env["send"](callback_update(OWNER, Admin(section="ulist", action="uf", id=7).pack()))
    result = fake.last(EditMessageText)
    assert FA("adm_ulist_empty") in result.text
    assert FA("adm_ud_operations") not in [s for s, _ in fake.buttons(result)]
    assert cust.telegram_id


async def test_profile_permissions_and_foreign_customer(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"])
    support = StaffIdentity(StaffRole.SUPPORT, None, frozenset(rbac.permissions_for(StaffRole.SUPPORT)))
    text, kb = await admin.customer_card(session, FA, cust, support)
    sections = [Admin.unpack(b.callback_data).section for row in kb.inline_keyboard for b in row]
    assert "adjust" not in sections and "ugive" not in sections and "utrust" not in sections
    assert "uprofile" in sections and "home" in sections
    assert ("upay" in sections) == support.can("payments.read")
    other = await factories.shop(session, "Foreign detail")
    foreign = await factories.customer(session, other, username="secret_foreign")
    await session.commit()
    for section in ("user", "uprofile", "upay"):
        await env["send"](callback_update(OWNER, Admin(section=section, id=foreign.id).pack()))
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert "secret_foreign" not in " ".join(env["fake"].texts())


async def payment(session, cust, suffix, **kw):  # type: ignore[no-untyped-def]
    row = Payment(
        shop_id=cust.shop_id,
        customer_id=cust.id,
        purpose=PaymentPurpose.TOPUP,
        provider="card",
        status=PaymentStatus.PAID,
        amount=123_456,
        currency="IRT",
        idempotency_key=f"detail-payment-{cust.id}-{suffix}",
        **kw,
    )
    session.add(row)
    await session.flush()
    return row


async def test_payments_paginate_and_hide_secrets(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"], username="<unsafe>")
    rows = [
        await payment(session, cust, i, meta={"secret": "PRIVATE_META"}, receipt_text="PRIVATE_RECEIPT")
        for i in range(12)
    ]
    another = await factories.customer(session, env["shop"], username="someone_else")
    outsider = await payment(session, another, 0)
    await session.commit()
    send, fake = env["send"], env["fake"]
    await send(callback_update(OWNER, Admin(section="upay", id=cust.id).pack()))
    result = fake.last(EditMessageText)
    assert "&lt;unsafe&gt;" in result.text and "<unsafe>" not in result.text
    assert result.text.count("• #") == 10
    assert f"#{outsider.id} ·" not in result.text
    assert "PRIVATE_" not in result.text
    assert FA("adm_ud_payment_paid") in result.text
    assert Admin(section="upay", id=cust.id, page=1).pack() in [v for _, v in fake.buttons(result)]
    await send(callback_update(OWNER, Admin(section="upay", id=cust.id, page=999).pack()))
    result = fake.last(EditMessageText)
    assert result.text.count("• #") == 2 and f"#{rows[0].id} ·" in result.text
    assert Admin(section="user", id=cust.id).pack() in [v for _, v in fake.buttons(result)]


async def test_empty_payments_and_input_origin_preserved(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"])
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="upay", id=cust.id).pack()))
    assert FA("adm_ud_payment_empty") in env["fake"].last(EditMessageText).text
    state = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    assert isinstance(state, FSMContext)
    origin = {"customer_id": cust.id, "shop_id": cust.shop_id, "filter": 1, "page": 3, "action": "uf"}
    await state.update_data(user_list_origin=origin, customer_id=cust.id, amount=100)
    await clear_customer_input(state)
    assert await state.get_data() == {"user_list_origin": origin}
    assert await state.get_state() is None


async def test_invalid_profile_origin_and_permission_forgery(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"])
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="user", action="uf999", id=cust.id).pack()))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    # A staff identity with customers.read only must not access the payments handler.
    from paurubot.bot.routers.admin_customer import router

    handler = next(h for h in router.callback_query.handlers if h.callback.__name__ == "payment_history")
    staff = StaffIdentity(StaffRole.SUPPORT, None, frozenset({"customers.read"}))
    from aiogram.types import CallbackQuery

    call = callback_update(SUPPORT, Admin(section="upay", id=cust.id).pack()).callback_query
    assert isinstance(call, CallbackQuery)
    ok, _ = await handler.check(call, staff=staff)
    assert not ok


async def test_referrals_paginated_and_shop_scoped(session, env):  # type: ignore[no-untyped-def]
    parent = await factories.customer(session, env["shop"], username="parent")
    children = [await factories.customer(session, env["shop"], username=f"child_{i}") for i in range(12)]
    for child in children:
        child.referrer_id = parent.id
    other = await factories.shop(session, "Different shop")
    foreign = await factories.customer(session, other, username="foreign_referral")
    foreign.referrer_id = parent.id  # Defense against inconsistent historical/imported links.
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="uref", id=parent.id).pack()))
    fake = env["fake"]
    result = fake.last(EditMessageText)
    assert result.text.count("• ") == 10 and "foreign_referral" not in result.text
    assert Admin(section="uref", id=parent.id, page=1).pack() in [v for _, v in fake.buttons(result)]
    await env["send"](callback_update(OWNER, Admin(section="uref", id=parent.id, page=99).pack()))
    result = fake.last(EditMessageText)
    assert result.text.count("• ") == 2 and "foreign_referral" not in result.text


async def test_origin_not_reused_for_another_customer(session, env):  # type: ignore[no-untyped-def]
    first = await factories.customer(session, env["shop"], username="one")
    second = await factories.customer(session, env["shop"], username="two")
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="user", action="uf7", id=first.id).pack()))
    await env["send"](callback_update(OWNER, Admin(section="uprofile", id=second.id).pack()))
    result = env["fake"].last(EditMessageText)
    assert FA("adm_ulist_all") in result.text and "@two" in [s for s, _ in env["fake"].buttons(result)]
