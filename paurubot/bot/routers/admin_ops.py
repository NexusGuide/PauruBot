"""Running the shop from the bot (OPS-09): a health check, backups on demand and sending an
archive here, and log groups set up with a command inside the group (topics included)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import record
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers.admin import actor
from paurubot.bot.utils import StaffOnly, show
from paurubot.core.formatting import esc, jdate, size
from paurubot.db.models import LogChannel
from paurubot.db.models import Shop as ShopRow
from paurubot.i18n import Texts
from paurubot.worker import queue

router = Router(name="admin_ops")
router.message.filter(StaffOnly())
router.callback_query.filter(StaffOnly())

LEVEL_ICON = {"ok": "🟢", "warn": "🟡", "fail": "🔴"}
SHOWN_BACKUPS = 5
GROUPS = {"group", "supergroup"}


def ops_buttons(t: Texts, staff: StaffIdentity) -> list[tuple[str, Admin]]:
    from paurubot.bot.routers.admin_hub import hub_buttons

    out: list[tuple[str, Admin]] = []
    if staff.can("settings.write"):
        out.append((t("adm_status"), Admin(section="status")))
    if staff.can("backups.manage"):
        out.append((t("adm_backups"), Admin(section="backups")))
    out.extend(hub_buttons(t, staff))
    return out


@router.callback_query(Admin.filter(F.section == "status"), StaffOnly("settings.write"))
async def status(call: CallbackQuery, t: Texts) -> None:
    from paurubot.ops import doctor

    await call.answer(t("adm_status_wait"))
    checks = await doctor.run(with_telegram=False)
    lines = [t("adm_status_title")]
    lines += [f"{LEVEL_ICON.get(c.level, '⚪️')} <b>{esc(c.key)}</b>: {esc(c.detail)[:200]}" for c in checks]
    kb = InlineKeyboardBuilder()
    kb.button(text=t("btn_refresh"), callback_data=Admin(section="status"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(2)
    await show(call, "\n".join(lines), kb.as_markup())


@router.callback_query(Admin.filter(F.section == "backups"), StaffOnly("backups.manage"))
async def backups(
    call: CallbackQuery,
    callback_data: Admin,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    from paurubot.config import get_settings
    from paurubot.ops import backup, delivery

    if shop.parent_id is not None:
        await call.answer(t("adm_forbidden"), show_alert=True)
        return
    items = backup.listing()[:SHOWN_BACKUPS]
    if callback_data.action == "now":
        if not get_settings().backup_passphrase.get_secret_value():
            await call.answer(t("adm_backup_no_passphrase"), show_alert=True)
            return
        job = await queue.enqueue(
            session, "ops", "backup", {"label": "manual"}, shop_id=shop.id, dedupe_key=f"backup:bot:{call.id}"
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="backup.requested",
            entity="job",
            entity_id=job,
        )
        await session.commit()
        await call.answer(t("adm_backup_queued"), show_alert=True)
        return
    if callback_data.action == "get":
        if not 0 <= callback_data.page < len(items):
            await call.answer(t("button_expired"), show_alert=True)
            return
        info = items[callback_data.page]
        await call.answer()
        if not isinstance(call.message, Message):
            return
        if info.size > delivery.TELEGRAM_FILE_LIMIT:
            await call.message.answer(t("backup_too_large", name=esc(info.name), size=size(info.size)))
            return
        await call.message.answer_document(
            FSInputFile(info.path, filename=info.name),
            caption=t("backup_caption", name=esc(info.name), size=size(info.size)),
            protect_content=True,
        )
        await record(
            session,
            shop_id=shop.id,
            actor=actor(staff),
            action="backup.downloaded",
            entity="backup",
            after={"name": info.name, "via": "bot"},
        )
        await session.commit()
        return
    await call.answer()
    lines = [t("adm_backups_title")]
    if not items:
        lines.append(t("adm_backups_empty"))
    kb = InlineKeyboardBuilder()
    for index, info in enumerate(items):
        lines.append(f"• {esc(info.name)} — {size(info.size)} — {jdate(info.created_at, with_time=True)}")
        kb.button(
            text=t("adm_backup_get", n=index + 1),
            callback_data=Admin(section="backups", action="get", page=index),
        )
    kb.button(text=t("adm_backup_now"), callback_data=Admin(section="backups", action="now"))
    kb.button(text=t("btn_back"), callback_data=Admin(section="home"))
    kb.adjust(*([3] * ((len(items) + 2) // 3)), 1, 1)
    await show(call, "\n".join(lines), kb.as_markup())


# --- log groups -----------------------------------------------------------------------------


def _topics() -> tuple[str, ...]:
    from paurubot.api.routes.admin.system import LOG_TOPICS

    return LOG_TOPICS


@router.message(Command("setlog"), F.chat.type.in_(GROUPS), StaffOnly("settings.write"))
async def set_log(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """`/setlog orders` inside a group (or one of its topics) sends that kind of report there."""
    topic = (command.args or "").strip().lower()
    if topic not in _topics():
        await message.reply(t("adm_setlog_usage", topics=", ".join(_topics())))
        return
    thread_id = message.message_thread_id if message.is_topic_message else None
    row = await session.scalar(
        select(LogChannel).where(LogChannel.shop_id == shop.id, LogChannel.topic == topic)
    )
    if row is None:
        row = LogChannel(shop_id=shop.id, topic=topic, chat_id=message.chat.id, thread_id=thread_id)
        session.add(row)
    else:
        row.chat_id, row.thread_id = message.chat.id, thread_id
    await session.flush()
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="channel.log_set",
        entity="channel",
        entity_id=row.id,
        after={"topic": topic, "chat_id": message.chat.id, "thread_id": thread_id},
    )
    await session.commit()
    await message.reply(t("adm_setlog_done", topic=topic))


@router.message(Command("unsetlog"), F.chat.type.in_(GROUPS), StaffOnly("settings.write"))
async def unset_log(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    topic = (command.args or "").strip().lower()
    row = await session.scalar(
        select(LogChannel).where(
            LogChannel.shop_id == shop.id, LogChannel.topic == topic, LogChannel.chat_id == message.chat.id
        )
    )
    if row is None:
        await message.reply(t("adm_unsetlog_none"))
        return
    await session.delete(row)
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="channel.log_remove",
        entity="channel",
        entity_id=row.id,
    )
    await session.commit()
    await message.reply(t("adm_unsetlog_done", topic=topic))


@router.message(Command("logs"), F.chat.type.in_(GROUPS), StaffOnly("settings.write"))
async def list_logs(message: Message, session: AsyncSession, shop: ShopRow, t: Texts) -> None:
    rows = list(
        await session.scalars(
            select(LogChannel).where(LogChannel.shop_id == shop.id, LogChannel.chat_id == message.chat.id)
        )
    )
    if not rows:
        await message.reply(t("adm_logs_none", topics=", ".join(_topics())))
        return
    await message.reply(t("adm_logs_here", topics=", ".join(sorted(r.topic for r in rows))))


@router.message(Command("setuplogs"), F.chat.type.in_(GROUPS), StaffOnly("settings.write"))
async def setup_logs(
    message: Message,
    session: AsyncSession,
    shop: ShopRow,
    t: Texts,
    staff: StaffIdentity,
) -> None:
    """Create all missing forum topics in this supergroup and bind every report topic."""
    from paurubot.core import log_topics

    try:
        res = await log_topics.auto_setup_forum_topics(session, shop.id, message.chat.id)
    except log_topics.TopicSetupError as exc:
        await message.reply(t(f"adm_setuplogs_err_{exc.code}"))
        return
    await record(
        session,
        shop_id=shop.id,
        actor=actor(staff),
        action="channel.logs_auto_setup",
        entity="channel",
        after={"chat_id": message.chat.id, **res},
    )
    await session.commit()
    await message.reply(
        t(
            "adm_setuplogs_done",
            created=len(res["created"]),
            skipped=len(res["skipped"]),
            failed=len(res["failed"]),
        )
    )
