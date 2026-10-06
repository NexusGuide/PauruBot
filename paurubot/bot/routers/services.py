from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import keyboards
from paurubot.bot.callbacks import Svc
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ServiceStates
from paurubot.bot.utils import Btn, show
from paurubot.bot.views import qr_png, service_card
from paurubot.core.formatting import esc
from paurubot.db.models import Customer, Service, ServiceStatus
from paurubot.i18n import Texts
from paurubot.panels import PanelError
from paurubot.provisioning import lifecycle

log = logging.getLogger(__name__)
router = Router(name="services")
PAGE = 8


async def owned(session: AsyncSession, customer: Customer, service_id: int) -> Service | None:
    service = await session.get(Service, service_id)
    if service is None or service.customer_id != customer.id or service.status == ServiceStatus.DELETED:
        return None
    return service


def _service_markup(t: Texts, service: Service, cfg: dict[str, Any]):  # type: ignore[no-untyped-def]
    return keyboards.service_kb(
        t, service, extras=bool(cfg["extras_enabled"]), upgrade=bool(cfg["upgrade_enabled"])
    )


async def list_services(
    event: Message | CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, page: int = 0
) -> None:
    base = select(Service).where(Service.customer_id == customer.id, Service.status != ServiceStatus.DELETED)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    if not total:
        await show(event, t("no_services"))
        return
    rows = list(await session.scalars(base.order_by(Service.id.desc()).offset(page * PAGE).limit(PAGE)))
    await show(
        event,
        t("services_header", count=total),
        keyboards.services_kb(t, rows, page, (page + 1) * PAGE < total),
    )


@router.message(Btn("btn_services"))
async def services_entry(
    message: Message, session: AsyncSession, customer: Customer, t: Texts, state: FSMContext
) -> None:
    await state.clear()
    await list_services(message, session, customer, t)


@router.callback_query(Svc.filter(F.action == "list"))
async def services_page(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    await call.answer()
    await list_services(call, session, customer, t, max(0, callback_data.page))


@router.callback_query(Svc.filter(F.action == "view"))
async def view(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    try:
        await lifecycle.refresh(session, service)
    except PanelError as exc:
        log.info("refresh failed for service %s: %s", service.id, exc)
    await session.commit()
    await call.answer()
    if service.status == ServiceStatus.DELETED:
        await show(call, t("service_removed_from_panel"))
        return
    await show(call, service_card(t, service), _service_markup(t, service, cfg))


@router.callback_query(Svc.filter(F.action == "links"))
async def links(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    items = await lifecycle.links(session, service)
    if not isinstance(call.message, Message):
        return
    if not items:
        await call.message.answer(t("links_empty", sub=esc(service.subscription_url or "-")))
        return
    body = "\n\n".join(f"<code>{esc(link)}</code>" for link in items[:12])
    await call.message.answer(t("links_header") + "\n\n" + body, disable_web_page_preview=True)


@router.callback_query(Svc.filter(F.action == "qr"))
async def qr(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None or not service.subscription_url:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer_photo(
            BufferedInputFile(qr_png(service.subscription_url), "qr.png"), caption=t("qr_caption")
        )


@router.callback_query(Svc.filter(F.action == "revoke"))
async def revoke_ask(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    await show(
        call,
        t("revoke_confirm"),
        keyboards.confirm(
            t, Svc(action="revoke_yes", id=service.id).pack(), Svc(action="view", id=service.id).pack()
        ),
    )


@router.callback_query(Svc.filter(F.action == "revoke_yes"))
async def revoke(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    try:
        await lifecycle.revoke(session, service)
    except PanelError as exc:
        log.warning("revoke failed for service %s: %s", service.id, exc)
        await call.answer(t("panel_error"), show_alert=True)
        return
    await session.commit()
    await call.answer(t("revoked"))
    await show(call, service_card(t, service), _service_markup(t, service, cfg))


@router.callback_query(Svc.filter(F.action == "rename"))
async def rename_ask(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    state: FSMContext,
) -> None:
    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    await state.set_state(ServiceStates.rename)
    await state.update_data(service_id=service.id)
    if isinstance(call.message, Message):
        await call.message.answer(t("ask_new_label"), reply_markup=keyboards.cancel(t))


@router.message(ServiceStates.rename, F.text)
async def rename(
    message: Message,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
    staff: Any,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    service = await owned(session, customer, int(data.get("service_id") or 0))
    await state.clear()
    if service is None:
        await message.answer(t("service_not_found"), reply_markup=menu(t, cfg, staff))
        return
    service.label = (message.text or "").strip()[:48] or None
    await message.answer(t("label_saved"), reply_markup=menu(t, cfg, staff))
    await message.answer(
        service_card(t, service), reply_markup=_service_markup(t, service, cfg), disable_web_page_preview=True
    )
