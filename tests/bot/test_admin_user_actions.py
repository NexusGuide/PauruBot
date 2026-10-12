"""The staff user card's one-tap actions: referrals, card to card, identity mark, account
transfer, services on/off, zero balance, channels, cron messages and reseller controls."""

from __future__ import annotations

from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from paurubot.auth import rbac
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers import admin
from paurubot.db.models import AuditLog, Customer, ResellerProfile, ServiceStatus, StaffRole
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from tests import factories
from tests.bot.test_customer_controls import FA, OWNER, UID, _buy, alerts, labels
from tests.bot.test_customer_controls import env as env  # noqa: F401  (fixture)
from tests.fakes.telegram import callback_update, message_update

_payments = iter(range(880_000, 889_999))


async def _owner_card(session, env, customer):  # type: ignore[no-untyped-def]
    await env["send"](message_update(OWNER, "/start"))
    await env["send"](callback_update(OWNER, Admin(section="user", id=customer.id).pack()))
    return env["fake"].last(EditMessageText)


def _sections(call) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    out = []
    for row in call.reply_markup.inline_keyboard:
        for button in row:
            data = Admin.unpack(button.callback_data)
            out.append((data.section, data.action))
    return out


async def _actions(session, customer_id: int) -> list[str]:  # type: ignore[no-untyped-def]
    rows = await session.scalars(
        select(AuditLog.action).where(AuditLog.entity == "customer", AuditLog.entity_id == customer_id)
    )
    return list(rows)


async def test_card_layout_follows_the_requested_order(session, env) -> None:  # type: ignore[no-untyped-def]
    shop = env["shop"]
    referrer = await factories.customer(session, shop)
    customer = await factories.customer(session, shop, telegram_id=UID)
    customer.referrer_id = referrer.id
    await session.commit()
    call = await _owner_card(session, env, customer)
    found = labels(env["fake"])
    expected = [
        "adm_user_btn_referrals",
        "adm_ua_btn_unref",
        "adm_ua_btn_clear_refs",
        "adm_ua_btn_card_on",
        "adm_ua_btn_kyc_on",
        "adm_ua_btn_kyc_off",
        "adm_ua_btn_card_off",
        "adm_user_btn_give",
        "adm_user_btn_trial",
        "adm_ud_payments",
        "adm_ua_btn_move",
        "adm_ua_btn_power_off",
        "adm_ua_btn_power_on",
        "adm_ua_btn_channels",
        "adm_ua_btn_zero",
    ]
    assert found[: len(expected)] == [FA(key) for key in expected]
    assert FA("adm_ua_btn_cron", state=FA("adm_ua_on")) in found
    assert FA("adm_user_btn_hide") in found and FA("adm_ua_btn_hidden") in found
    assert FA("adm_ua_btn_rbot_del") not in found and FA("adm_ua_btn_expiry") not in found
    assert FA("adm_ua_kyc_no") in call.text
    assert all(len(b.callback_data.encode()) <= 64 for row in call.reply_markup.inline_keyboard for b in row)


async def test_support_sees_only_allowed_actions(session, env) -> None:  # type: ignore[no-untyped-def]
    customer = await factories.customer(session, env["shop"])
    support = StaffIdentity(StaffRole.SUPPORT, None, frozenset(rbac.permissions_for(StaffRole.SUPPORT)))
    _, kb = await admin.customer_card(session, FA, customer, support)
    sections = {Admin.unpack(b.callback_data).section for row in kb.inline_keyboard for b in row}
    assert not sections & {"ucard", "uzero", "umove", "adjust", "ubase", "udemote"}
    assert {"ukyc", "ucron", "uchan", "upower"} <= sections


async def test_referral_removal(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, send = env["shop"], env["send"]
    boss = await factories.customer(session, shop)
    customer = await factories.customer(session, shop, telegram_id=UID)
    children = [await factories.customer(session, shop) for _ in range(3)]
    customer.referrer_id = boss.id
    for child in children:
        child.referrer_id = customer.id
    await session.commit()
    await _owner_card(session, env, customer)
    await send(callback_update(OWNER, Admin(section="urefx", action="unref", id=customer.id).pack()))
    await session.refresh(customer)
    assert customer.referrer_id is None and FA("adm_ua_unref_done") in alerts(env["fake"])

    await send(callback_update(OWNER, Admin(section="urefx", action="clear", id=customer.id).pack()))
    assert FA("btn_yes") in labels(env["fake"])
    for child in children:
        await session.refresh(child)
        assert child.referrer_id == customer.id  # nothing before the confirmation
    await send(callback_update(OWNER, Admin(section="urefx", action="go", id=customer.id).pack()))
    for child in children:
        await session.refresh(child)
        assert child.referrer_id is None
    assert FA("adm_ua_clear_refs_done", count=3) in alerts(env["fake"])
    assert {"customer.referrer_removed", "customer.referrals_cleared"} <= set(
        await _actions(session, customer.id)
    )


async def test_card_and_identity_and_cron(session, env) -> None:  # type: ignore[no-untyped-def]
    send = env["send"]
    customer = await factories.customer(session, env["shop"], telegram_id=UID)
    await session.commit()
    await _owner_card(session, env, customer)
    await send(callback_update(OWNER, Admin(section="ucard", action="off", id=customer.id).pack()))
    await session.refresh(customer)
    assert customer.card_trust == "blocked"
    await send(callback_update(OWNER, Admin(section="ucard", action="on", id=customer.id).pack()))
    await session.refresh(customer)
    assert customer.card_trust == "normal"
    await send(callback_update(OWNER, Admin(section="ucard", action="on", id=customer.id).pack()))
    assert FA("adm_ua_card_already_on") in alerts(env["fake"])

    await send(callback_update(OWNER, Admin(section="ukyc", action="on", id=customer.id).pack()))
    await session.refresh(customer)
    assert "verified" in customer.tags
    assert FA("adm_ua_kyc_yes") in env["fake"].last(EditMessageText).text
    await send(callback_update(OWNER, Admin(section="ukyc", action="off", id=customer.id).pack()))
    await session.refresh(customer)
    assert "verified" not in customer.tags

    await send(callback_update(OWNER, Admin(section="ucron", id=customer.id).pack()))
    await session.refresh(customer)
    assert customer.alerts_muted is True
    assert FA("adm_ua_btn_cron", state=FA("adm_ua_off")) in labels(env["fake"])


async def test_zero_balance_after_confirmation(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, send = env["shop"], env["send"]
    customer = await factories.customer(session, shop, telegram_id=UID)
    await wallet.topup(session, shop.id, customer.id, 45_000, provider="card", payment_id=next(_payments))
    await session.commit()
    await _owner_card(session, env, customer)
    await send(callback_update(OWNER, Admin(section="uzero", id=customer.id).pack()))
    assert await wallet_balance(session, shop.id, customer.id) == 45_000
    await send(callback_update(OWNER, Admin(section="uzero", action="go", id=customer.id).pack()))
    assert await wallet_balance(session, shop.id, customer.id) == 0
    await send(callback_update(OWNER, Admin(section="uzero", id=customer.id).pack()))
    assert FA("adm_ua_zero_already") in alerts(env["fake"])


async def test_account_transfer_moves_services_and_balance(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, send, fake = env["shop"], env["send"], env["fake"]
    source = await factories.customer(session, shop, telegram_id=UID)
    target = await factories.customer(session, shop, telegram_id=7904)
    service = await _buy(session, env, source)
    await wallet.topup(session, shop.id, source.id, 30_000, provider="card", payment_id=next(_payments))
    await session.commit()
    await _owner_card(session, env, source)
    await send(callback_update(OWNER, Admin(section="umove", id=source.id).pack()))
    await send(message_update(OWNER, str(source.telegram_id)))
    assert FA("adm_ua_move_self") in fake.texts(OWNER)
    await send(message_update(OWNER, "999999"))
    assert FA("adm_ua_move_no_target") in fake.texts(OWNER)
    await send(message_update(OWNER, str(target.telegram_id)))
    confirm = fake.last(SendMessage)
    yes = next(data for text, data in fake.buttons(confirm) if text == FA("btn_yes"))
    await session.refresh(service)
    assert service.customer_id == source.id  # not before the confirmation
    await send(callback_update(OWNER, yes))
    await session.refresh(service)
    assert service.customer_id == target.id
    assert await wallet_balance(session, shop.id, source.id) == 0
    assert await wallet_balance(session, shop.id, target.id) == 30_000
    assert "customer.account_moved" in await _actions(session, source.id)
    await send(callback_update(OWNER, yes))  # pressed again: nothing left to move twice
    assert await wallet_balance(session, shop.id, target.id) == 30_000


async def test_account_power_off_and_on(session, env) -> None:  # type: ignore[no-untyped-def]
    send = env["send"]
    customer = await factories.customer(session, env["shop"], telegram_id=UID)
    first, second = await _buy(session, env, customer), await _buy(session, env, customer)
    await _owner_card(session, env, customer)
    await send(callback_update(OWNER, Admin(section="upower", action="off", id=customer.id).pack()))
    await session.refresh(first)
    assert first.status != ServiceStatus.DISABLED  # asks first
    await send(callback_update(OWNER, Admin(section="upower", action="off", id=customer.id, page=1).pack()))
    for service in (first, second):
        await session.refresh(service)
        assert service.status == ServiceStatus.DISABLED
    assert FA("adm_ua_power_done", done=2, failed=0) in alerts(env["fake"])
    await send(callback_update(OWNER, Admin(section="upower", action="on", id=customer.id).pack()))
    for service in (first, second):
        await session.refresh(service)
        assert service.status != ServiceStatus.DISABLED


async def test_channel_check_without_channels(session, env) -> None:  # type: ignore[no-untyped-def]
    customer = await factories.customer(session, env["shop"], telegram_id=UID)
    await session.commit()
    await _owner_card(session, env, customer)
    await env["send"](callback_update(OWNER, Admin(section="uchan", id=customer.id).pack()))
    assert FA("adm_ua_chan_none") in alerts(env["fake"])


async def test_reseller_controls(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, send, fake = env["shop"], env["send"], env["fake"]
    customer = await factories.customer(session, shop, telegram_id=UID)
    profile = ResellerProfile(shop_id=shop.id, customer_id=customer.id, permissions=[])
    session.add(profile)
    customer.hidden_panel_ids = [env["panel"].id]
    await session.commit()
    await session.refresh(customer)
    await _owner_card(session, env, customer)
    found = labels(fake)
    for key in (
        "adm_user_btn_rbot",
        "adm_ua_btn_rbot_del",
        "adm_ua_btn_base_gb",
        "adm_ua_btn_base_hour",
        "adm_ua_btn_hide_reseller",
        "adm_ua_btn_hidden",
        "adm_ua_btn_expiry",
        "adm_ua_btn_demote",
    ):
        assert FA(key) in found, key
    assert FA("adm_ua_btn_move") not in found  # a reseller account isn't transferred

    await send(callback_update(OWNER, Admin(section="ubase", action="gb", id=customer.id).pack()))
    await send(message_update(OWNER, "2500"))
    await send(callback_update(OWNER, Admin(section="ubase", action="hour", id=customer.id).pack()))
    await send(message_update(OWNER, "120"))
    await session.refresh(profile)
    assert (profile.gb_price, profile.hourly_price) == (2500, 120)

    await send(callback_update(OWNER, Admin(section="uexp", id=customer.id).pack()))
    await send(message_update(OWNER, "30"))
    await session.refresh(profile)
    assert profile.expires_at is not None
    await send(callback_update(OWNER, Admin(section="uexp", id=customer.id).pack()))
    await send(message_update(OWNER, "0"))
    await session.refresh(profile)
    assert profile.expires_at is None

    await send(callback_update(OWNER, Admin(section="urbotdel", id=customer.id).pack()))
    assert FA("adm_ua_rbot_none") in alerts(fake)

    await send(callback_update(OWNER, Admin(section="uhidden", id=customer.id).pack()))
    assert f"👁 {env['panel'].name}" in labels(fake)
    await send(
        callback_update(
            OWNER, Admin(section="uhidden", action="unhide", id=customer.id, page=env["panel"].id).pack()
        )
    )
    await session.refresh(customer)
    assert customer.hidden_panel_ids == []

    await send(callback_update(OWNER, Admin(section="udemote", id=customer.id).pack()))
    await session.refresh(profile)
    assert profile.is_active
    await send(callback_update(OWNER, Admin(section="udemote", action="go", id=customer.id).pack()))
    await session.refresh(profile)
    assert not profile.is_active
    assert FA("adm_ua_btn_demote") not in labels(fake)


async def test_other_shop_customer_is_refused(session, env) -> None:  # type: ignore[no-untyped-def]
    other = await factories.shop(session, "Elsewhere")
    foreign = await factories.customer(session, other)
    await session.commit()
    await env["send"](message_update(OWNER, "/start"))
    for section, action in (
        ("ukyc", "on"),
        ("uzero", "go"),
        ("urefx", "go"),
        ("ucron", "show"),
        ("umove", "show"),
    ):
        await env["send"](callback_update(OWNER, Admin(section=section, action=action, id=foreign.id).pack()))
        assert alerts(env["fake"])[-1] == FA("adm_customer_not_found"), section
    row = await session.get(Customer, foreign.id)
    assert row is not None and not row.alerts_muted and "verified" not in (row.tags or [])
