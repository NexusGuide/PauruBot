"""ClockBot parity, batch 2 (panel)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from paurubot.db.models import AuditLog, LogChannel, Plan
from tests import factories
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_a_plan_is_cloned_as_a_hidden_copy(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    plan = await factories.plan(session, env.shop, env.panel, price=99_000, title="۵۰ گیگ")
    await session.commit()
    owner = await login(http, "u_owner")
    made = await owner.call("POST", f"/api/admin/plans/{plan.id}/clone")
    assert made.status_code == 200, made.text
    copy = made.json()
    assert copy["id"] != plan.id and copy["price"] == 99_000 and copy["panel_id"] == env.panel.id
    assert copy["is_active"] is False and copy["title"].startswith("۵۰ گیگ")
    assert await session.scalar(select(AuditLog.id).where(AuditLog.action == "plan.clone"))
    other = await factories.plan(session, env.other_shop, env.other_panel)
    await session.commit()
    assert (await owner.call("POST", f"/api/admin/plans/{other.id}/clone")).status_code == 404
    assert len(list(await session.scalars(select(Plan.id).where(Plan.shop_id == env.shop.id)))) == 2


async def test_settings_and_texts_go_out_to_a_file_and_back(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import BotText

    await shop_settings.set_many(
        session, env.shop.id, {"min_topup": 30_000, "zarinpal_merchant": "secret-merchant-id-123"}
    )
    session.add(BotText(shop_id=env.shop.id, locale="fa", key="welcome", value="سلام از فایل"))
    await session.commit()
    owner = await login(http, "u_owner")
    exported = (await owner.call("GET", "/api/admin/settings/export")).json()
    assert exported["format"] == "paurubot-settings" and exported["settings"]["min_topup"] == 30_000
    assert "zarinpal_merchant" not in exported["settings"]  # secrets never leave
    assert {"locale": "fa", "key": "welcome", "value": "سلام از فایل"} in exported["texts"]

    exported["settings"]["min_topup"] = 40_000
    exported["settings"]["zarinpal_merchant"] = "should-not-import"
    exported["settings"]["not_a_setting"] = 1
    exported["texts"] = [{"locale": "fa", "key": "welcome", "value": "سلام تازه"}]
    done = await owner.call("POST", "/api/admin/settings/import", exported)
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["texts"] == 1 and result["skipped"] == 2  # the secret and the unknown key
    assert result["settings"] == len(exported["settings"]) - 2
    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert cfg["min_topup"] == 40_000 and cfg["zarinpal_merchant"] == "secret-merchant-id-123"
    text = await session.scalar(
        select(BotText.value).where(BotText.shop_id == env.shop.id, BotText.key == "welcome")
    )
    assert text == "سلام تازه"
    bad = await owner.call(
        "POST", "/api/admin/settings/import", {"format": "other", "settings": {}, "texts": []}
    )
    assert bad.status_code == 422
    support = await login(http, "u_support")
    assert (await support.call("POST", "/api/admin/settings/import", exported)).status_code == 403


async def test_stuck_orders_retry_or_mark_delivered(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from datetime import timedelta

    from sqlalchemy import update

    from paurubot.db.base import utcnow
    from paurubot.db.models import Job, JobStatus, Order, OrderKind, OrderStatus

    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    old = utcnow() - timedelta(hours=1)
    stuck = Order(
        shop_id=env.shop.id,
        customer_id=env.customer_id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        base_price=1000,
        amount=1000,
        created_at=old,
        updated_at=old,
    )
    fresh = Order(
        shop_id=env.shop.id,
        customer_id=env.customer_id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        base_price=1000,
        amount=1000,
    )
    failed = Order(
        shop_id=env.shop.id,
        customer_id=env.customer_id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.FAILED,
        base_price=1000,
        amount=1000,
        error="panel down",
    )
    session.add_all([stuck, fresh, failed])
    await session.commit()
    owner = await login(http, "u_owner")
    ids = [o["id"] for o in (await owner.call("GET", "/api/admin/stuck-orders")).json()["items"]]
    assert stuck.id in ids and failed.id in ids and fresh.id not in ids
    assert (await owner.call("POST", f"/api/admin/orders/{stuck.id}/retry")).status_code == 200
    assert await session.scalar(select(Job.id).where(Job.dedupe_key == f"order:{stuck.id}:provision"))
    done = await owner.call("POST", f"/api/admin/orders/{failed.id}/mark-delivered", {"note": "sent by hand"})
    assert done.status_code == 200 and done.json()["order"]["status"] == "completed"
    assert (
        await owner.call("POST", f"/api/admin/orders/{failed.id}/mark-delivered", {"note": "x"})
    ).status_code == 409
    finance = await login(http, "u_finance")
    assert (await finance.call("POST", f"/api/admin/orders/{stuck.id}/retry")).status_code == 403


async def test_verify_a_backup_and_a_separate_backup_bot(session, http, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from pathlib import Path

    from paurubot.core import settings as shop_settings
    from paurubot.ops import backup

    async def fake_verify(path: Path, *, passphrase=None, full=False):  # type: ignore[no-untyped-def]
        return {"objects": 42, "master_key_included": True, "manifest": {"revision": "x"}}

    monkeypatch.setattr(backup, "resolve", lambda name: Path("/tmp") / name)
    monkeypatch.setattr(backup, "verify", fake_verify)
    owner = await login(http, "u_owner")
    done = await owner.call("POST", "/api/admin/backups/pb-2026.tar.enc/verify")
    assert done.status_code == 200 and done.json() == {"ok": True, "objects": 42, "master_key_included": True}

    saved = await owner.call(
        "PUT",
        "/api/admin/backups/telegram",
        {"bot_token": "123456:ABCdefGhIJKlmNoPQRsTUVwxyZ1234567890", "chat_ids": [-1001234]},
    )
    assert saved.status_code == 200 and saved.json() == {"bot": True, "chat_ids": [-1001234]}
    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert cfg["backup_chat_ids"] == [-1001234] and cfg["backup_bot_token"]
    assert (
        await owner.call("PUT", "/api/admin/backups/telegram", {"bot_token": "nope", "chat_ids": []})
    ).status_code == 422
    support = await login(http, "u_support")
    assert (await support.call("POST", "/api/admin/backups/x/verify")).status_code == 403


async def test_backups_fall_back_to_the_main_bot_when_the_backup_bot_reaches_no_chat(
    session,
    env,  # noqa: F811
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import settings as shop_settings
    from paurubot.notifications import telegram
    from paurubot.ops import delivery
    from paurubot.ops.backup import BackupInfo

    await shop_settings.set_many(
        session,
        env.shop.id,
        {"backup_bot_token": "123456:ABCdefGhIJKlmNoPQRsTUVwxyZ1234567890", "backup_chat_ids": [-1001234]},
    )
    session.add(LogChannel(shop_id=env.shop.id, topic="backups", chat_id=-1009999))
    await session.commit()
    own: list[int] = []
    main: list[int] = []

    async def unreachable(token, chats, info):  # type: ignore[no-untyped-def]
        own.extend(chats)
        return 0

    async def send_document(chat_id, document, **kwargs):  # type: ignore[no-untyped-def]
        main.append(chat_id)

    monkeypatch.setattr(delivery, "_send_with_own_bot", unreachable)
    monkeypatch.setattr(telegram, "send_document", send_document)
    archive = tmp_path / "pb.tar.enc"
    archive.write_bytes(b"x")
    info = BackupInfo(name=archive.name, path=archive, size=1, created_at=None)  # type: ignore[arg-type]
    sent = await delivery.send_backup(info)
    assert own == [-1001234]
    assert sent == 1 and main == [-1009999], "the main bot must deliver when the backup bot can't"
