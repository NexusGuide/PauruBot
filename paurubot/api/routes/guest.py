"""Guest purchase pages: /buy and the private purchase page /buy/t/<token> (CAT-08)."""

from __future__ import annotations

import logging
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from paurubot import kv
from paurubot.api.deps import DB, request_ip
from paurubot.bot.views import qr_base64
from paurubot.config import get_settings
from paurubot.core import guest
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import days_left, jdate, money, size
from paurubot.db.models import OrderStatus, PaymentStatus, Shop
from paurubot.i18n import LOCALES, Texts, texts_for
from paurubot.provisioning import subscription

log = logging.getLogger(__name__)
router = APIRouter(tags=["guest"])

GUEST_COOKIE = "pb_guest"
GUEST_COOKIE_AGE = 30 * 86400
STARTS_PER_IP = 5
STARTS_WINDOW = 600
STARTS_PER_SHOP_HOUR = 200
OPEN = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.VERIFYING)


def _not_found() -> Response:
    return Response("not found", status_code=404, media_type="text/plain")


def _locale(request: Request, shop: Shop, saved: str | None = None) -> str:
    asked = request.query_params.get("lang") or saved or ""
    if asked in LOCALES:
        return asked
    header = request.headers.get("accept-language", "").lower()
    if header.startswith("en"):
        return "en"
    return shop.default_locale if shop.default_locale in LOCALES else "fa"


def _page(t: Texts, title: str, body: str, *, refresh: str = "", status: int = 200) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{escape(refresh)}">' if refresh else ""
    html = f"""<!doctype html>
<html lang="{escape(t.locale)}" dir="{"rtl" if t.locale == "fa" else "ltr"}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">{meta}
<title>{escape(title)}</title><link rel="stylesheet" href="/s/_/page.css"></head>
<body><main><h1>{escape(title)}</h1>{body}</main><script src="/s/_/page.js"></script></body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


async def _store(db: DB, request: Request, shop: Shop, t: Texts, error: str = "") -> HTMLResponse:
    title = shop.title or "PauruBot"
    plans = await guest.plans(db, shop.id)
    methods = await guest.methods(db, shop.id)
    if not plans or not methods:
        return _page(t, title, f'<section class="card"><p>{escape(t("guest_closed"))}</p></section>')
    options = "".join(
        f'<label class="opt"><input type="radio" name="plan_id" value="{p.id}" required{" checked" if i == 0 else ""}>'
        f"<span><b>{escape(p.title)}</b>"
        f"<small>{escape(t('plan_specs', days=p.days or t('unlimited'), gb=p.data_gb or t('unlimited')))}</small>"
        f'<span class="price">{escape(t("guest_price", price=money(p.price)))}</span></span></label>'
        for i, p in enumerate(plans)
    )
    ways = "".join(
        f'<label class="opt"><input type="radio" name="method" value="{escape(m)}" required'
        f"{' checked' if i == 0 else ''}><span>{escape(t(f'btn_pay_{m}'))}</span></label>"
        for i, m in enumerate(methods)
    )
    notice = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    bot = (
        f'<p class="note"><a href="https://t.me/{escape(shop.bot_username)}">{escape(t("guest_prefer_bot"))}</a></p>'
        if shop.bot_username
        else ""
    )
    body = f"""{notice}
<form method="post" action="/buy/{shop.id}?lang={escape(t.locale)}">
<section class="card"><h2>{escape(t("guest_pick_plan"))}</h2>{options}</section>
<section class="card"><h2>{escape(t("guest_pick_method"))}</h2>{ways}
<button type="submit" class="btn primary">{escape(t("guest_pay"))}</button></section>
</form>
<p class="note">{escape(t("guest_how"))}</p>{bot}"""
    return _page(t, title, body, status=400 if error else 200)


@router.get("/buy", include_in_schema=False)
@router.get("/buy/{shop_id:int}", include_in_schema=False)
async def store(request: Request, db: DB, shop_id: int | None = None) -> Response:
    shop = await guest.shop_for(db, shop_id)
    if shop is None:
        return _not_found()
    t = await texts_for(db, shop.id, _locale(request, shop))
    return await _store(db, request, shop, t)


@router.post("/buy/{shop_id:int}", include_in_schema=False)
async def buy(
    shop_id: int,
    request: Request,
    db: DB,
    ip: Annotated[str, Depends(request_ip)],
    plan_id: Annotated[int, Form(gt=0)],
    method: Annotated[str, Form(max_length=32)],
) -> Response:
    shop = await guest.shop_for(db, shop_id)
    if shop is None:
        return _not_found()
    t = await texts_for(db, shop.id, _locale(request, shop))
    allowed, _ = await kv.hit(f"guest:start:{guest.visitor_key(ip)}", STARTS_PER_IP, STARTS_WINDOW)
    if allowed:  # many addresses at once still cannot flood the shop with half-made purchases
        allowed, _ = await kv.hit(f"guest:start:shop:{shop.id}", STARTS_PER_SHOP_HOUR, 3600)
    if not allowed:
        return await _store(db, request, shop, t, t("guest_too_many"))
    try:
        async with db.begin_nested():  # a refused attempt leaves no half-made guest behind
            checkout = await guest.start(db, shop.id, plan_id, method, t.locale)
    except guest.GuestError as exc:
        text = t(exc.key)
        return await _store(db, request, shop, t, text if text != exc.key else t("gateway_error"))
    await db.commit()
    response = RedirectResponse(f"/buy/t/{checkout.customer.guest_token}?go=1", status_code=303)
    settings = get_settings()
    response.set_cookie(
        GUEST_COOKIE,
        str(checkout.customer.guest_token),
        max_age=GUEST_COOKIE_AGE,
        httponly=True,
        secure=settings.is_production or settings.public_url.startswith("https://"),
        samesite="lax",  # sent when the gateway sends the buyer back with a plain link
        path="/buy",
    )
    return response


@router.get("/buy/return", include_in_schema=False)
async def back_from_gateway(request: Request, db: DB) -> Response:
    """Where gateways send a guest back; the purchase page comes from the buyer's own cookie."""
    customer = await guest.by_token(db, request.cookies.get(GUEST_COOKIE, ""))
    if customer is not None:
        return RedirectResponse(f"/buy/t/{customer.guest_token}", status_code=303)
    shop = await guest.shop_for(db, None)
    if shop is None:
        return _not_found()
    t = await texts_for(db, shop.id, _locale(request, shop))
    body = f'<section class="card"><p class="name">{escape(t("guest_return_lost"))}</p></section>'
    return _page(t, shop.title or "PauruBot", body)


@router.get("/buy/t/{token}", include_in_schema=False)
async def purchase(token: str, request: Request, db: DB) -> Response:
    customer = await guest.by_token(db, token)
    if customer is None:
        return _not_found()
    allowed, _ = await kv.hit(f"guest:page:{token}", 60, 60)
    if not allowed:
        return Response("slow down", status_code=429, media_type="text/plain", headers={"Retry-After": "60"})
    shop = await db.get(Shop, customer.shop_id)
    if shop is None:
        return _not_found()
    t = await texts_for(db, shop.id, _locale(request, shop, customer.locale))
    title = shop.title or "PauruBot"
    state = await guest.status(db, customer)
    order, payment, service = state["order"], state["payment"], state["service"]
    keep = f'<p class="note">{escape(t("guest_keep_link"))}</p>'
    again = f'<a class="btn ghost" href="/buy/{shop.id}?lang={escape(t.locale)}">{escape(t("guest_buy_again"))}</a>'

    if service is not None and order is not None and order.status == OrderStatus.COMPLETED:
        cfg = await shop_settings.load(db, shop.id)
        link = subscription.customer_link(service, cfg) or ""
        page = subscription.branded_url(service)
        volume = size(service.data_limit) if service.data_limit else t("unlimited")
        ends = jdate(service.expire_at) if service.expire_at else t("unlimited")
        remaining = days_left(service.expire_at) if service.expire_at else None
        qr = qr_base64(link)
        image = (
            f'<img class="qr" src="data:image/png;base64,{qr}" alt="{escape(t("qr_caption"))}">' if qr else ""
        )
        claim = (
            f'<a class="btn primary" href="https://t.me/{escape(shop.bot_username)}?start=claim_{escape(token)}">'
            f'{escape(t("guest_add_to_telegram"))}</a><p class="note">{escape(t("guest_add_hint"))}</p>'
            if shop.bot_username
            else ""
        )
        open_page = (
            f'<a class="btn ghost" href="{escape(page)}">{escape(t("guest_open_page"))}</a>' if page else ""
        )
        body = f"""<section class="card"><p class="name">{escape(t("guest_ready"))}</p>
<div class="facts"><div>{escape(t("guest_volume"))}<b dir="ltr">{escape(volume)}</b></div>
<div>{escape(t("sub_page_expiry"))}<b>{escape(ends)}</b></div>
<div>{escape(t("sub_page_remaining"))}<b>{escape(t("sub_page_days", days=remaining) if remaining is not None else "-")}</b></div>
</div></section>
<section class="card">{image}
<div class="link">{escape(link)}</div>
<button type="button" class="btn primary" data-copy="{escape(link)}" data-done="{escape(t("sub_page_copied"))}">{escape(t("sub_page_copy"))}</button>
{open_page}</section>
<section class="card">{claim}{again}</section>{keep}"""
        return _page(t, title, body)

    if payment is not None and payment.status in OPEN:
        url = str((payment.meta or {}).get("redirect_url") or "")
        if not url.startswith("https://"):
            url = ""  # never a javascript: or plain-http link on the page, whatever a gateway returned
        go = request.query_params.get("go") == "1" and bool(url)
        button = f'<a class="btn primary" href="{escape(url)}">{escape(t("guest_go_pay"))}</a>' if url else ""
        body = f"""<section class="card"><p class="name">{escape(t("guest_waiting"))}</p>
<p class="status">{escape(t("guest_amount", amount=money(_to_pay(payment))))}</p>{button}</section>{keep}"""
        return _page(t, title, body, refresh=f"0;url={url}" if go else "10")

    paid = payment is not None and payment.status == PaymentStatus.PAID
    if paid or (order is not None and order.status in (OrderStatus.PAID, OrderStatus.PROVISIONING)):
        body = f'<section class="card"><p class="name">{escape(t("guest_building"))}</p></section>{keep}'
        return _page(t, title, body, refresh="5")

    reason = (
        "guest_review" if payment is not None and payment.status == PaymentStatus.DISPUTED else "guest_failed"
    )
    body = f'<section class="card"><p class="name">{escape(t(reason))}</p>{again}</section>{keep}'
    return _page(t, title, body)


def _to_pay(payment) -> int:  # type: ignore[no-untyped-def]
    from paurubot.payments.engine import amount_to_pay

    return amount_to_pay(payment)
