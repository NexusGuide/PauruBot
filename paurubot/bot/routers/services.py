from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.bot import infocard, keyboards
from paurubot.bot.callbacks import Svc
from paurubot.bot.keyboards import STATUS_ICON
from paurubot.bot.routers.start import menu
from paurubot.bot.states import ServiceStates
from paurubot.bot.utils import Btn, show
from paurubot.bot.views import qr_png, service_card
from paurubot.core.formatting import esc
from paurubot.core.orders import MOVABLE, service_extras
from paurubot.db.models import Customer, Service, ServiceStatus
from paurubot.i18n import Texts
from paurubot.panels import PanelError
from paurubot.panels.registry import delivery_of
from paurubot.provisioning import lifecycle
from paurubot.provisioning.subscription import customer_link

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
        t,
        service,
        extras=service_extras(cfg, service),
        upgrade=bool(cfg["upgrade_enabled"]),
        move=bool(cfg["move_enabled"]) and service.status in MOVABLE,
        card=bool(cfg["info_card_enabled"]),
        cancel=bool(cfg["self_delete_enabled"]),
        link=customer_link(service, cfg),
    )


async def list_services(
    event: Message | CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, page: int = 0
) -> None:
    from paurubot.core import stock

    base = select(Service).where(Service.customer_id == customer.id, Service.status != ServiceStatus.DELETED)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    has_stock = bool(await stock.for_customer(session, customer.id, limit=1))
    if not total and not has_stock:
        await show(event, t("no_services"))
        return
    rows = list(await session.scalars(base.order_by(Service.id.desc()).offset(page * PAGE).limit(PAGE)))
    await show(
        event,
        t("services_header", count=total),
        keyboards.services_kb(
            t,
            rows,
            page,
            (page + 1) * PAGE < total,
            stock=has_stock,
            muted=customer.alerts_muted,
            all_links=total > 1,
        ),
    )


@router.callback_query(Svc.filter(F.action == "stock"))
async def stock_list(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    """Ready-made configs this customer bought (CAT-05)."""
    from paurubot.core import stock

    items = await stock.for_customer(session, customer.id, limit=10)
    await call.answer()
    if not items:
        await show(call, t("no_services"))
        return
    parts = [t("stock_header")]
    for item in items:
        config = stock.payload(item)
        shown = config if len(config) <= 600 else config[:600] + "…"
        parts.append(f"#{item.id}\n<code>{esc(shown)}</code>")
    await show(call, "\n\n".join(parts)[:4000], keyboards.back_to_services(t))


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
    await show(call, service_card(t, service, customer_link(service, cfg)), _service_markup(t, service, cfg))


@router.callback_query(Svc.filter(F.action == "mute"))
async def toggle_alerts(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    """The customer turns its own expiry and data reminders off or on (SVC-15)."""
    customer.alerts_muted = not customer.alerts_muted
    await session.commit()
    await call.answer(t("alerts_muted") if customer.alerts_muted else t("alerts_unmuted"), show_alert=True)
    await list_services(call, session, customer, t)


@router.callback_query(Svc.filter(F.action == "cancel"))
async def cancel_service(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    """Deleting one's own service (SVC-14): what comes back, then a confirmation."""
    from paurubot.bot.callbacks import Svc as SvcData
    from paurubot.core import cancellation
    from paurubot.core.formatting import money

    service = await owned(session, customer, callback_data.id)
    if service is None or not cfg["self_delete_enabled"]:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    refund = await cancellation.quote(session, customer, service)
    text = t(
        "cancel_service_confirm_refund" if refund else "cancel_service_confirm",
        name=esc(service.label or service.panel_username),
        amount=money(refund),
    )
    await show(
        call,
        text,
        keyboards.confirm(
            t,
            SvcData(action="cancel_go", id=service.id).pack(),
            SvcData(action="view", id=service.id).pack(),
        ),
    )


@router.callback_query(Svc.filter(F.action == "cancel_go"))
async def cancel_service_go(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    from paurubot.core import cancellation
    from paurubot.core.formatting import money

    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    try:
        refund = await cancellation.cancel(session, customer, service)
    except cancellation.CancelError as exc:
        await call.answer(t(exc.key), show_alert=True)
        return
    except PanelError:
        await session.rollback()
        await call.answer(t("panel_busy"), show_alert=True)
        return
    await session.commit()
    await call.answer()
    await show(
        call, t("cancel_service_done_refund", amount=money(refund)) if refund else t("cancel_service_done")
    )


@router.callback_query(Svc.filter(F.action == "report"))
async def report_problem(
    call: CallbackQuery, callback_data: Svc, session: AsyncSession, customer: Customer, t: Texts
) -> None:
    """A customer's "it doesn't work": a ticket with the service's state attached, and an honest
    answer when its panel is already known to be down (SUP-05)."""
    from paurubot import kv
    from paurubot.core import tickets
    from paurubot.core.formatting import jdate
    from paurubot.db.models import Panel
    from paurubot.panels import GB
    from paurubot.worker import queue

    service = await owned(session, customer, callback_data.id)
    if service is None:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    allowed, _ = await kv.hit(f"report:{service.id}", 1, 1800)
    if not allowed:
        await call.answer(t("report_wait"), show_alert=True)
        return
    try:
        await lifecycle.refresh(session, service)
    except PanelError as exc:
        log.info("refresh failed for reported service %s: %s", service.id, exc)
    panel = await session.get(Panel, service.panel_id)
    down = panel is not None and (panel.is_healthy is False or not panel.is_active)
    details = t(
        "report_details",
        name=esc(service.label or service.panel_username),
        username=esc(service.panel_username),
        status=t(f"status_{service.status.value}"),
        used=round(int(service.used_bytes) / GB, 2),
        limit=round(int(service.data_limit) / GB, 2) if service.data_limit else t("unlimited"),
        expires=jdate(service.expire_at) if service.expire_at else t("unlimited"),
        panel=esc(panel.name if panel else "-"),
        health=t("report_panel_down") if down else t("report_panel_up"),
    )
    try:
        ticket = await tickets.open_ticket(
            session,
            customer,
            t("report_subject", name=service.label or service.panel_username),
            source="report",
        )
        await tickets.add_message(
            session, ticket, author_type="customer", author_id=customer.id, text=details
        )
    except tickets.TicketError as exc:
        await call.answer(t(exc.key), show_alert=True)
        return
    await queue.enqueue(
        session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=customer.shop_id
    )
    await session.commit()
    await call.answer()
    await show(call, t("report_known_outage" if down else "report_sent", id=ticket.id))


async def _send_wireguard(message: Message, t: Texts, service: Service, config: str | None) -> None:
    """A WireGuard service is a .conf file and its QR, not a link (PNL-08)."""
    if not config:
        await message.answer(t("wireguard_unavailable"))
        return
    name = esc(service.label or service.panel_username)
    filename = f"{service.panel_username}.conf"
    await message.answer_document(
        BufferedInputFile(config.encode(), filename), caption=t("wireguard_caption", name=name)
    )
    png = qr_png(config)
    if png is not None:
        await message.answer_photo(BufferedInputFile(png, "wireguard.png"))


@router.callback_query(Svc.filter(F.action == "links"))
async def links(
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
    await call.answer()
    if not isinstance(call.message, Message):
        return
    kind = delivery_of(service.panel.kind if service.panel else None)
    if kind == "account":
        await show(call, service_card(t, service, None), _service_markup(t, service, cfg))
        return
    items = await lifecycle.links(session, service)
    if kind == "wireguard":
        await _send_wireguard(call.message, t, service, items[0] if items else None)
        return
    if not items:
        await call.message.answer(
            t("links_empty", sub=esc(customer_link(service, cfg) or "-")), disable_web_page_preview=True
        )
        return
    body = "\n\n".join(f"<code>{esc(link)}</code>" for link in items[:12])
    await call.message.answer(t("links_header") + "\n\n" + body, disable_web_page_preview=True)


ALL_LIMIT = 100
ALL_FILE_LIMIT = 30
MESSAGE_ROOM = 3800


async def _all_services(session: AsyncSession, customer: Customer, limit: int) -> list[Service]:
    rows = await session.scalars(
        select(Service)
        .where(Service.customer_id == customer.id, Service.status != ServiceStatus.DELETED)
        .order_by(Service.id.desc())
        .limit(limit)
    )
    return list(rows)


@router.callback_query(Svc.filter(F.action == "all"))
async def all_links(
    call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts, cfg: dict[str, Any]
) -> None:
    """Every service's link in one message (SVC-17); a file when they don't fit in one."""
    items = await _all_services(session, customer, ALL_LIMIT)
    if not items:
        await call.answer(t("no_services"), show_alert=True)
        return
    await call.answer()
    if not isinstance(call.message, Message):
        return
    blocks: list[str] = []
    plain: list[str] = []
    for service in items:
        name = service.label or service.panel_username
        link = customer_link(service, cfg) or "-"
        blocks.append(f"{STATUS_ICON.get(service.status, '')} <b>{esc(name)}</b>\n<code>{esc(link)}</code>")
        plain.append(f"{name}\n{link}")
    kb = keyboards.single(t("btn_all_configs_file"), Svc(action="all_file").pack())
    text = t("all_links_header", count=len(items)) + "\n\n" + "\n\n".join(blocks)
    if len(text) <= MESSAGE_ROOM:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        return
    await call.message.answer_document(
        BufferedInputFile("\n\n".join(plain).encode(), "links.txt"),
        caption=t("all_links_file", count=len(items)),
        reply_markup=kb,
    )


@router.callback_query(Svc.filter(F.action == "all_file"))
async def all_configs_file(call: CallbackQuery, session: AsyncSession, customer: Customer, t: Texts) -> None:
    """Every config of every service, asked from the panels, in one file (SVC-17)."""
    items = await _all_services(session, customer, ALL_FILE_LIMIT)
    if not items:
        await call.answer(t("no_services"), show_alert=True)
        return
    await call.answer(t("all_configs_wait"))
    if not isinstance(call.message, Message):
        return
    parts: list[str] = []
    for service in items:
        if delivery_of(service.panel.kind if service.panel else None) != "subscription":
            continue  # WireGuard files and panel logins are sent from the service itself
        if not service.subscription_url:
            continue
        try:
            links = await lifecycle.links(session, service)
        except Exception:  # one unreachable panel must not cost the customer the rest
            log.warning("links for service %s failed", service.id)
            continue
        if links:
            parts.append(f"# {service.label or service.panel_username}\n" + "\n".join(links))
    if not parts:
        await call.message.answer(t("all_configs_empty"))
        return
    await call.message.answer_document(
        BufferedInputFile("\n\n".join(parts).encode(), "configs.txt"),
        caption=t("all_configs_caption", count=len(parts)),
    )


@router.callback_query(Svc.filter(F.action == "qr"))
async def qr(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    service = await owned(session, customer, callback_data.id)
    kind = delivery_of(service.panel.kind if service is not None and service.panel else None)
    if service is not None and kind == "account":
        await call.answer(t("account_no_qr"), show_alert=True)
        return
    if service is not None and kind == "wireguard":
        await call.answer()
        if isinstance(call.message, Message):
            items = await lifecycle.links(session, service)
            await _send_wireguard(call.message, t, service, items[0] if items else None)
        return
    link = customer_link(service, cfg) if service is not None else None
    if service is None or not link:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    if isinstance(call.message, Message):
        png = qr_png(link)
        if png is None:
            await call.message.answer(t("qr_too_long"))
        else:
            await call.message.answer_photo(BufferedInputFile(png, "qr.png"), caption=t("qr_caption"))


@router.callback_query(Svc.filter(F.action == "card"))
async def info_card(
    call: CallbackQuery,
    callback_data: Svc,
    session: AsyncSession,
    customer: Customer,
    t: Texts,
    cfg: dict[str, Any],
) -> None:
    """A picture of the service with usage, days left and the QR (BOT-06)."""
    service = await owned(session, customer, callback_data.id)
    if service is None or not cfg["info_card_enabled"]:
        await call.answer(t("service_not_found"), show_alert=True)
        return
    await call.answer()
    if not isinstance(call.message, Message):
        return
    kind = delivery_of(service.panel.kind if service.panel else None)
    scan: str | None = customer_link(service, cfg)
    if kind == "account":
        scan = None
    elif kind == "wireguard":
        items = await lifecycle.links(session, service)
        scan = items[0] if items else None
    try:
        image = await infocard.make(t, service, scan, cfg, call.bot)
    except (OSError, ValueError) as exc:
        log.warning("info card for service %s failed: %s", service.id, exc)
        await call.message.answer(t("infocard_failed"))
        return
    name = esc(service.label or service.panel_username)
    await call.message.answer_photo(
        BufferedInputFile(image, "card.png"), caption=t("infocard_caption", name=name)
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
    await show(call, service_card(t, service, customer_link(service, cfg)), _service_markup(t, service, cfg))


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
        service_card(t, service, customer_link(service, cfg)),
        reply_markup=_service_markup(t, service, cfg),
        disable_web_page_preview=True,
    )
