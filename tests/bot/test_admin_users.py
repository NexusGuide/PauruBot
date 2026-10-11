"""User hub through the real dispatcher, including confirmed shop-scoped bulk operations."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select, update

import paurubot.notifications.handlers  # noqa: F401
from paurubot.auth import rbac
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers import admin_hub, admin_users
from paurubot.core import service_gifts
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    Job,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    Service,
    ServiceStatus,
    StaffRole,
    VerifiedCard,
)
from paurubot.i18n import Texts, catalog, fields
from paurubot.ledger import wallet
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_provisioning import _buy, drain

FA = Texts("fa")
OWNER, SUPPORT, OTHER_ADMIN = 7911, 7912, 7913


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Admin users")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    fake_panel = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake_panel)
    for role, uid in ((StaffRole.OWNER, OWNER), (StaffRole.SUPPORT, SUPPORT), (StaffRole.ADMIN, OTHER_ADMIN)):
        await create_staff(
            session, shop.id, role=role, username=f"users_{uid}", password=None, telegram_id=uid
        )
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {
        "shop": shop,
        "panel": panel,
        "plan": plan,
        "panel_fake": fake_panel,
        "send": send,
        "fake": fake,
        "dp": dp,
        "bot": bot,
    }
    await bot.session.close()
    registry.test_transports.clear()
    registry._cache.clear()


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [label for label, _ in fake.buttons(fake.last(kind))]


def first_target(fake, kind):  # type: ignore[no-untyped-def]
    target = fake.buttons(fake.last(kind))[0][1]
    assert target
    return target


async def draft(session, env, mode="gb", value="5"):  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(callback_update(OWNER, Admin(section="ubulk", action=mode).pack()))
    await send(message_update(OWNER, value))
    target = first_target(fake, SendMessage)
    number = Admin.unpack(target).id
    assert number > 0
    return number, target


async def service(session, env):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, env["shop"], username="gift_customer")
    await wallet.topup(session, cust.shop_id, cust.id, 200_000, provider="card", payment_id=cust.id)
    await _buy(session, cust, OrderKind.PURCHASE, plan=env["plan"])
    await drain(session, "provision")
    row = await session.scalar(select(Service).where(Service.customer_id == cust.id))
    assert row is not None
    return row


def test_user_menu_layout_and_permissions() -> None:
    owner = StaffIdentity(StaffRole.OWNER, None, frozenset(rbac.permissions_for(StaffRole.OWNER)))
    kb = admin_users.home_kb(FA, owner)
    assert [len(row) for row in kb.inline_keyboard] == [1, 1, 1, 1, 1, 2, 2, 2, 1, 2]
    support = StaffIdentity(StaffRole.SUPPORT, None, frozenset(rbac.permissions_for(StaffRole.SUPPORT)))
    labels = [b.text for row in admin_users.home_kb(FA, support).inline_keyboard for b in row]
    assert FA("adm_users_bulk") not in labels
    assert FA("adm_users_charge") not in labels
    assert FA("adm_users_order") in labels
    for i, key in enumerate(admin_hub.USER_FILTERS):
        if key == "cards":
            assert i == 7  # Existing callback indexes must not change.


def test_new_locale_keys_and_placeholders_match() -> None:
    fa, en = catalog("fa"), catalog("en")
    keys = {k for k in fa if k.startswith("adm_users_")}
    assert keys == {k for k in en if k.startswith("adm_users_")}
    for key in keys | {"adm_ulist_cards"}:
        assert key in fa and key in en
        assert fields(fa[key]) == fields(en[key])


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#۱۲۳", "123"),
        ("123", "123"),
        ("alice_1", "alice_1"),
        ("0", None),
        ("-1", None),
        ("", None),
        ("a b", None),
        ("9" * 64, None),
    ],
)
def test_order_query_validation(value: str, expected: str | None) -> None:
    assert admin_users.order_query(value) == expected


async def test_user_hub_from_landing_and_filtered_back(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, FA("adm_nav_users")))
    assert FA("adm_users_cards") in labels(fake, SendMessage)
    await send(callback_update(OWNER, Admin(section="ulist", action="uf", id=0).pack()))
    assert Admin(section="users").pack() in [v for _, v in fake.buttons(fake.last(EditMessageText))]
    await send(callback_update(OWNER, Admin(section="users").pack()))
    assert FA("adm_users_order") in labels(fake)


async def test_approved_card_filter_no_duplicates_and_no_foreign_users(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, send, fake = env["shop"], env["send"], env["fake"]
    approved = await factories.customer(session, shop, username="approved_user")
    pending = await factories.customer(session, shop, username="pending_user")
    other_shop = await factories.shop(session, "Other card shop")
    foreign = await factories.customer(session, other_shop, username="foreign_user")
    for i, (cust, status) in enumerate(
        ((approved, "approved"), (approved, "approved"), (pending, "pending"), (foreign, "approved"))
    ):
        session.add(
            VerifiedCard(
                shop_id=cust.shop_id,
                customer_id=cust.id,
                last4="1234",
                masked="****1234",
                digest=f"d{i}",
                status=status,
            )
        )
    await session.commit()
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="ulist", action="uf", id=7).pack()))
    shown = labels(fake)
    assert sum("approved_user" in text for text in shown) == 1
    assert not any("pending_user" in text or "foreign_user" in text for text in shown)


@pytest.mark.security
async def test_order_lookup_number_username_details_and_scope(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    order = await session.scalar(select(Order).where(Order.service_id == row.id))
    assert order is not None
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    for query in (f"#{order.id}", row.panel_username):
        await send(callback_update(OWNER, Admin(section="uorders", action="show").pack()))
        await send(message_update(OWNER, query))
        assert any(f"#{order.id}" in label for label in labels(fake, SendMessage))
    await send(callback_update(OWNER, Admin(section="uorders", action="view", id=order.id).pack()))
    assert row.customer.display_name in fake.last(EditMessageText).text
    assert FA("adm_users_customer_card") in labels(fake)
    other_shop = await factories.shop(session, "Other order shop")
    cust = await factories.customer(session, other_shop)
    foreign = Order(
        shop_id=other_shop.id,
        customer_id=cust.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        amount=321,
    )
    session.add(foreign)
    await session.commit()
    assert await admin_users.find_orders(session, env["shop"].id, str(foreign.id), 0) == []
    await send(callback_update(OWNER, Admin(section="uorders", action="view", id=foreign.id).pack()))
    assert fake.last(AnswerCallbackQuery).show_alert


@pytest.mark.security
async def test_bulk_requires_preview_confirmation_and_does_not_repeat(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    before = row.data_limit
    draft_id, confirmation = await draft(session, env)
    await session.refresh(row)
    assert row.data_limit == before
    parent = await session.get(Job, draft_id)
    assert parent and parent.status == JobStatus.DRAFT
    assert not await session.scalar(select(Job.id).where(Job.dedupe_key.like(f"bulk:bot:{draft_id}:%")))
    await send(callback_update(OWNER, confirmation))
    await send(callback_update(OWNER, confirmation))
    children = list(await session.scalars(select(Job).where(Job.dedupe_key.like(f"bulk:bot:{draft_id}:%"))))
    assert len(children) == 1
    await drain(session, "bulk")
    await session.refresh(row)
    assert row.data_limit == before + 5 * GB
    # Requeue the same durable child; fixed targets must not add another 5 GB.
    child = children[0]
    await session.refresh(child)
    child.status, child.run_at = JobStatus.QUEUED, utcnow()
    await session.commit()
    await drain(session, "bulk")
    await session.refresh(row)
    assert row.data_limit == before + 5 * GB
    await send(callback_update(OWNER, Admin(section="ubulk", action="status", id=draft_id).pack()))
    assert "اعمال‌شده: 1" in fake.last(EditMessageText).text
    audit = await session.scalar(
        select(AuditLog).where(AuditLog.action == "service.bulk_extend", AuditLog.shop_id == env["shop"].id)
    )
    assert audit and audit.after["draft_id"] == draft_id


async def test_bulk_days_and_snapshot_exclude_new_services(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    before = row.expire_at
    await env["send"](message_update(OWNER, "/start"))
    number, confirmation = await draft(session, env, "days", "۳")
    cust = await factories.customer(session, env["shop"])
    newcomer = Service(
        shop_id=cust.shop_id,
        customer_id=cust.id,
        panel_id=env["panel"].id,
        panel_username="new_after_preview",
        status=ServiceStatus.ACTIVE,
        data_limit=GB,
        expire_at=utcnow() + timedelta(days=20),
    )
    session.add(newcomer)
    await session.commit()
    await env["send"](callback_update(OWNER, confirmation))
    children = list(await session.scalars(select(Job).where(Job.dedupe_key.like(f"bulk:bot:{number}:%"))))
    assert len(children) == 1 and children[0].payload["service_id"] == row.id
    await drain(session, "bulk")
    await session.refresh(row)
    assert before and row.expire_at and row.expire_at - before == timedelta(days=3)


@pytest.mark.security
async def test_bulk_cancellation_expiry_creator_and_permission_checks(session, env) -> None:  # type: ignore[no-untyped-def]
    await service(session, env)
    send = env["send"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(SUPPORT, "/start"))
    await send(message_update(OTHER_ADMIN, "/start"))
    number, confirmation = await draft(session, env)
    await send(callback_update(SUPPORT, confirmation))
    await send(callback_update(OTHER_ADMIN, confirmation))
    parent = await session.get(Job, number, populate_existing=True)
    assert parent and parent.status == JobStatus.DRAFT
    await send(callback_update(OWNER, Admin(section="ubulk", action="cancel", id=number).pack()))
    await session.refresh(parent)
    assert parent.status == JobStatus.CANCELED
    number, confirmation = await draft(session, env)
    parent = await session.get(Job, number)
    assert parent
    parent.created_at = utcnow() - timedelta(minutes=16)
    await session.commit()
    await send(callback_update(OWNER, confirmation))
    await session.refresh(parent)
    assert parent.status == JobStatus.CANCELED
    assert not await session.scalar(select(Job.id).where(Job.dedupe_key.like(f"bulk:bot:{number}:%")))


@pytest.mark.security
async def test_bulk_skips_changed_or_banned_service(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    send = env["send"]
    await send(message_update(OWNER, "/start"))
    number, confirmation = await draft(session, env)
    row.data_limit += GB  # An independent adjustment after preview must not be overwritten.
    await session.commit()
    newer_limit = row.data_limit
    await send(callback_update(OWNER, confirmation))
    await drain(session, "bulk")
    await session.refresh(row)
    assert row.data_limit == newer_limit
    jobs = list(await session.scalars(select(Job).where(Job.dedupe_key.like(f"bulk:bot:{number}:%"))))
    assert jobs[0].payload["outcome"] == "skipped_changed"
    number, confirmation = await draft(session, env)
    row.customer.is_banned = True
    await session.commit()
    await send(callback_update(OWNER, confirmation))
    await drain(session, "bulk")
    await session.refresh(row)
    assert row.data_limit == newer_limit


async def test_gift_preview_excludes_trial_unlimited_banned_inactive_and_other_shop(session, env) -> None:  # type: ignore[no-untyped-def]
    normal = await service(session, env)
    original = await service_gifts.preview(session, env["shop"].id, "gb", 1)
    assert [t["service_id"] for t in original] == [normal.id]
    cust = await factories.customer(session, env["shop"])
    for i, options in enumerate(({"is_trial": True}, {"data_limit": 0}, {"status": ServiceStatus.DISABLED})):
        attrs = dict(
            shop_id=cust.shop_id,
            customer_id=cust.id,
            panel_id=env["panel"].id,
            panel_username=f"excluded{i}",
            status=ServiceStatus.ACTIVE,
            data_limit=GB,
        )
        attrs.update(options)
        session.add(Service(**attrs))
    other = await factories.shop(session, "Other services")
    other_panel = await factories.panel(session, other)
    foreign_cust = await factories.customer(session, other)
    session.add(
        Service(
            shop_id=other.id,
            customer_id=foreign_cust.id,
            panel_id=other_panel.id,
            panel_username="foreign",
            status=ServiceStatus.ACTIVE,
            data_limit=GB,
        )
    )
    await session.commit()
    assert [t["service_id"] for t in await service_gifts.preview(session, env["shop"].id, "gb", 1)] == [
        normal.id
    ]


@pytest.mark.security
async def test_wallet_credit_confirmation_bound_to_current_preview(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="gift_all").pack()))
    await send(message_update(OWNER, "50000"))
    await send(callback_update(OWNER, Admin(section="gift_all", action="aud", page=0).pack()))
    stale = first_target(fake, EditMessageText)
    await send(callback_update(OWNER, Admin(section="gift_all", action="show").pack()))
    await send(message_update(OWNER, "60000"))
    await send(callback_update(OWNER, Admin(section="gift_all", action="aud", page=0).pack()))
    current = first_target(fake, EditMessageText)
    assert stale != current
    await send(callback_update(OWNER, stale))
    assert fake.last(AnswerCallbackQuery).show_alert
    assert not await session.scalar(select(Job.id).where(Job.kind == "gift", Job.shop_id == env["shop"].id))
    await send(callback_update(OWNER, current))
    await send(callback_update(OWNER, current))
    jobs = list(await session.scalars(select(Job).where(Job.kind == "gift", Job.shop_id == env["shop"].id)))
    assert len(jobs) == 1 and jobs[0].payload["amount"] == 60000


async def test_order_pagination_and_invalid_input(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    for _ in range(12):
        session.add(
            Order(
                shop_id=row.shop_id,
                customer_id=row.customer_id,
                service_id=row.id,
                kind=OrderKind.RENEW,
                status=OrderStatus.COMPLETED,
                amount=1000,
            )
        )
    await session.commit()
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="uorders", action="show").pack()))
    await send(message_update(OWNER, "0"))
    assert fake.last(SendMessage).text == FA("adm_users_order_invalid")
    await send(message_update(OWNER, row.panel_username))
    assert FA("btn_next") in labels(fake, SendMessage)
    await send(callback_update(OWNER, Admin(section="uorders", action="page", page=1).pack()))
    assert FA("btn_prev") in labels(fake)
    assert FA("btn_next") not in labels(fake)


@pytest.mark.security
async def test_bulk_draft_cannot_cross_shop(session, env) -> None:  # type: ignore[no-untyped-def]
    await service(session, env)
    send = env["send"]
    await send(message_update(OWNER, "/start"))
    number, confirmation = await draft(session, env)
    other = await factories.shop(session, "Other draft shop")
    await create_staff(
        session,
        other.id,
        role=StaffRole.OWNER,
        username="other_draft_owner",
        password=None,
        telegram_id=OWNER,
    )
    await session.commit()
    build_dispatcher(MemoryStorage(), shop_id=other.id)
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, confirmation))
    parent = await session.get(Job, number, populate_existing=True)
    assert parent and parent.status == JobStatus.DRAFT
    assert not await session.scalar(select(Job.id).where(Job.dedupe_key.like(f"bulk:bot:{number}:%")))
    build_dispatcher(MemoryStorage(), shop_id=env["shop"].id)


async def test_bulk_deleted_service_is_reported_as_skipped(session, env) -> None:  # type: ignore[no-untyped-def]
    row = await service(session, env)
    await env["send"](message_update(OWNER, "/start"))
    number, confirmation = await draft(session, env)
    await env["send"](callback_update(OWNER, confirmation))
    row.status = ServiceStatus.DELETED
    await session.commit()
    await drain(session, "bulk")
    job = await session.scalar(select(Job).where(Job.dedupe_key.like(f"bulk:bot:{number}:%")))
    assert job and job.status == JobStatus.DONE and job.payload["outcome"] == "skipped_ineligible"


@pytest.mark.security
async def test_fixed_gift_job_checks_shop_before_contacting_panel(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.worker import queue

    row = await service(session, env)
    before = row.data_limit
    targets = await service_gifts.preview(session, env["shop"].id, "gb", 3)
    other = await factories.shop(session, "Wrong queue shop")
    number = await queue.enqueue(
        session,
        "bulk",
        "service",
        {"service_id": row.id, "action": "extend", "gift_target": targets[0]},
        shop_id=other.id,
    )
    await session.commit()
    await drain(session, "bulk")
    await session.refresh(row)
    assert row.data_limit == before
    job = await session.get(Job, number)
    assert job and job.status == JobStatus.DEAD


async def test_bulk_limits_and_unsupported_panels(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import PanelKind

    row = await service(session, env)
    for mode, value in (("gb", 0), ("days", 3651), ("bad", 2)):
        with pytest.raises(ValueError):
            await service_gifts.preview(session, env["shop"].id, mode, value)
    monkeypatch.setattr(service_gifts, "LIMIT", 0)
    with pytest.raises(ValueError, match="too many"):
        await service_gifts.preview(session, env["shop"].id, "gb", 1)
    monkeypatch.setattr(service_gifts, "LIMIT", 1000)
    row.panel.kind = PanelKind.IBSNG
    await session.commit()
    assert await service_gifts.preview(session, env["shop"].id, "gb", 1) == []


@pytest.mark.security
async def test_lost_panel_reply_recovers_without_another_gift(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.panels import PanelUnavailable, driver_for

    row = await service(session, env)
    before = row.data_limit
    target = (await service_gifts.preview(session, env["shop"].id, "gb", 2))[0]
    real = await driver_for(row.panel)

    class AmbiguousDriver:
        changes = real.changes
        calls = 0

        async def get_user(self, ref):  # type: ignore[no-untyped-def]
            return await real.get_user(ref)

        async def update_user(self, ref, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            await real.update_user(ref, **kwargs)
            raise PanelUnavailable("reply lost after successful update")

    ambiguous = AmbiguousDriver()

    async def patched(_):  # type: ignore[no-untyped-def]
        return ambiguous

    monkeypatch.setattr(service_gifts, "driver_for", patched)
    with pytest.raises(PanelUnavailable):
        await service_gifts.apply_fixed(session, row, target)
    assert row.data_limit == before
    assert await service_gifts.apply_fixed(session, row, target) == "already_applied"
    assert row.data_limit == before + 2 * GB and ambiguous.calls == 1
