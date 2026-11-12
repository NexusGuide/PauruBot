"""Foxima parity: broadcasts with bot-section buttons, per-panel and idle audiences, a final report
to the sender, and direct messages with media and a reply button. Our own options stay as they are."""

from __future__ import annotations

from datetime import timedelta

from aiogram.methods import CopyMessage, EditMessageText, SendMessage
from sqlalchemy import select

from paurubot.bot.callbacks import Admin, Menu
from paurubot.db.base import utcnow
from paurubot.db.models import Broadcast, Customer, Service, ServiceStatus
from tests.bot.test_admin_tools import FA, OWNER, env  # noqa: F401  (shared fixture)
from tests.fakes.telegram import callback_update, message_update, photo_update
from tests.unit.test_resellers import drain

BUYER, IDLE, FRESH = 7801, 7802, 7803


async def _customer(session, telegram_id):  # type: ignore[no-untyped-def]
    return await session.scalar(select(Customer).where(Customer.telegram_id == telegram_id))


async def _draft(send, session, text="Weekend sale"):  # type: ignore[no-untyped-def]
    await send(callback_update(OWNER, Admin(section="broadcast").pack()))
    await send(message_update(OWNER, text))
    return await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()))


async def test_a_broadcast_to_one_panels_buyers_with_a_buy_button_and_a_final_report(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    for uid in (OWNER, BUYER, IDLE):
        await send(message_update(uid, "/start"))
    buyer = await _customer(session, BUYER)
    session.add(
        Service(
            shop_id=env["shop"].id,
            customer_id=buyer.id,
            panel_id=env["panel"].id,
            panel_username="fx_buyer",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.commit()
    row = await _draft(send, session)
    await send(callback_update(OWNER, Admin(section="bc", action="aud_menu", id=row.id).pack()))
    await send(callback_update(OWNER, Admin(section="bc", action="aud_panel", id=row.id).pack()))
    assert env["panel"].name in [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    await send(
        callback_update(OWNER, Admin(section="bc", action="panel", id=row.id, page=env["panel"].id).pack())
    )
    await session.refresh(row)
    assert row.audience["kind"] == "panel" and row.audience["panel_id"] == env["panel"].id
    # A button that opens a section of the bot, beside our link button.
    await send(callback_update(OWNER, Admin(section="bc", action="act_menu", id=row.id).pack()))
    await send(callback_update(OWNER, Admin(section="bc", action="act", id=row.id, page=1).pack()))
    await session.refresh(row)
    assert row.buttons == [{"text": FA("btn_buy"), "action": "buy"}]
    await send(callback_update(OWNER, Admin(section="bc", action="send", id=row.id).pack()))
    fake.reset()
    await drain(session, "broadcast")
    copies = [c for c in fake.calls if isinstance(c, CopyMessage)]
    assert [c.chat_id for c in copies] == [BUYER]  # only that panel's buyers
    button = copies[0].reply_markup.inline_keyboard[0][0]
    assert button.callback_data == Menu(item="buy").pack()
    reports = [c for c in fake.calls if isinstance(c, SendMessage) and c.chat_id == OWNER]
    assert (
        reports
        and "#" + str(row.id) in reports[-1].text
        and FA("adm_bc_report", id=row.id, sent=1, total=1, failed=0, blocked=0, deleted=0, skipped=0)
        == reports[-1].text
    )  # the sender hears how it went


async def test_a_broadcast_to_people_who_have_not_used_the_bot_for_days(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    for uid in (OWNER, IDLE, FRESH):
        await send(message_update(uid, "/start"))
    idle = await _customer(session, IDLE)
    idle.last_seen_at = utcnow() - timedelta(days=40)
    await session.commit()
    row = await _draft(send, session)
    await send(callback_update(OWNER, Admin(section="bc", action="aud_idle", id=row.id).pack()))
    await send(message_update(OWNER, "30"))
    await session.refresh(row)
    assert row.audience["kind"] == "idle" and row.audience["idle_days"] == 30
    await send(callback_update(OWNER, Admin(section="bc", action="send", id=row.id).pack()))
    fake.reset()
    await drain(session, "broadcast")
    assert [c.chat_id for c in fake.calls if isinstance(c, CopyMessage)] == [IDLE]


async def test_a_direct_message_with_a_photo_and_a_reply_button(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(BUYER, "/start"))
    buyer = await _customer(session, BUYER)
    await send(callback_update(OWNER, Admin(section="umsg", id=buyer.id).pack()))
    await send(photo_update(OWNER, "photo-file-1", caption="Your new config is ready"))
    assert FA("adm_umsg_reply_yes") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    fake.reset()
    await send(callback_update(OWNER, Admin(section="umsgs", action="reply", id=buyer.id).pack()))
    copies = [c for c in fake.calls if isinstance(c, CopyMessage) and c.chat_id == BUYER]
    assert len(copies) == 1 and "Your new config is ready" in (copies[0].caption or "")
    assert copies[0].reply_markup.inline_keyboard[0][0].callback_data == Menu(item="tickets").pack()
    # Text keeps working, now with the same choice.
    await send(callback_update(OWNER, Admin(section="umsg", id=buyer.id).pack()))
    await send(message_update(OWNER, "Hello"))
    fake.reset()
    await send(callback_update(OWNER, Admin(section="umsgs", action="plain", id=buyer.id).pack()))
    sent = [c for c in fake.calls if isinstance(c, SendMessage) and c.chat_id == BUYER]
    assert len(sent) == 1 and "Hello" in sent[0].text and sent[0].reply_markup is None


def test_every_main_menu_section_can_be_a_broadcast_button() -> None:
    """Panel sale, reseller, Mini App, games and the rest: whatever the main menu holds."""
    from paurubot.bot.menu import BUTTONS
    from paurubot.core.broadcasts import ACTIONS

    assert set(ACTIONS) == {"home", *BUTTONS}
    assert ACTIONS[:8] == ("home", "buy", "services", "wallet", "trial", "referral", "tickets", "guide"), (
        "drafts and buttons made before keep their index"
    )


async def test_the_bot_offers_the_sections_this_shop_has_on(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from paurubot.core import settings as shop_settings
    from paurubot.core.broadcasts import ACTIONS

    send, fake = env["send"], env["fake"]
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {"panel_accounts_enabled": True, "lottery_enabled": False, "wheel_enabled": False},
    )
    await session.commit()
    shop_settings.invalidate()
    await send(message_update(OWNER, "/start"))
    row = await _draft(send, session)
    await send(callback_update(OWNER, Admin(section="bc", action="act_menu", id=row.id).pack()))
    labels = [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    assert FA("btn_accounts") in labels and FA("btn_reseller") in labels, (
        "panel sale and reseller are offered"
    )
    assert FA("btn_lottery") not in labels and FA("btn_wheel") not in labels, "sections switched off are not"
    await send(
        callback_update(
            OWNER, Admin(section="bc", action="act", id=row.id, page=ACTIONS.index("accounts")).pack()
        )
    )
    await session.refresh(row)
    assert row.buttons == [{"text": FA("btn_accounts"), "action": "accounts"}]
