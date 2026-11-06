"""Guest purchase pages: /buy and the private purchase page /buy/t/<token> (CAT-08)."""

from __future__ import annotations

import logging
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from paurubot import __version__, kv
from paurubot.api.deps import DB, request_ip
from paurubot.bot.views import qr_base64
from paurubot.config import get_settings
from paurubot.core import guest, guest_checkout
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import days_left, jdate, money, size
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, GuestCheckoutIntent, Order, OrderStatus, PaymentStatus, Shop
from paurubot.i18n import LOCALES, Texts, texts_for
from paurubot.payments import saved_checkout
from paurubot.provisioning import subscription
from paurubot.security import guest_form

log = logging.getLogger(__name__)
router = APIRouter(tags=["guest"])

GUEST_COOKIE = "pb_guest"
GUEST_COOKIE_AGE = 30 * 86400
STARTS_PER_IP = 5
STARTS_WINDOW = 600
STARTS_PER_SHOP_HOUR = 200
OPEN = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.VERIFYING)


def _intent_cookie(shop_id: int) -> tuple[str, bool]:
    settings = get_settings()
    secure = settings.is_production or settings.public_url.startswith("https://")
    # Host-only + Secure prevents sibling subdomains planting a matching form cookie.
    prefix = "__Host-pb_guest_intent_" if secure else "pb_guest_intent_"
    return f"{prefix}{shop_id}", secure


def _form_identity(request: Request, shop_id: int) -> str:
    name, _ = _intent_cookie(shop_id)
    previous = request.cookies.get(name, "")
    if request.query_params.get("new") != "1" and guest_form.valid(previous, shop_id):
        return previous
    return guest_form.issue(shop_id)


def _with_intent(response: HTMLResponse, shop_id: int, token: str) -> HTMLResponse:
    name, secure = _intent_cookie(shop_id)
    response.set_cookie(
        name, token, max_age=guest_form.TTL, httponly=True, secure=secure, samesite="strict", path="/"
    )
    return response


def _intent_notice(t: Texts, error: str) -> str:
    known = {
        "payment_intent_invalid": "guest_checkout_form_invalid",
        "payment_intent_conflict": "guest_checkout_form_conflict",
        "payment_intent_closed": "guest_checkout_form_closed",
    }
    if error in known:
        return t(known[error])
    text = t(error)
    return text if text != error else t("gateway_error")


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


def _page(
    t: Texts, title: str, body: str, *, refresh: str = "", status: int = 200, switch: str = ""
) -> HTMLResponse:
    """A guest page. `switch` is the address of this same page, for the language link."""
    meta = f'<meta http-equiv="refresh" content="{escape(refresh)}">' if refresh else ""
    other = "en" if t.locale == "fa" else "fa"
    lang = (
        f'<a class="lang" href="{escape(switch)}{"&" if "?" in switch else "?"}lang={other}">'
        f"{escape(t('guest_lang_switch'))}</a>"
        if switch
        else ""
    )
    html = f"""<!doctype html>
<html lang="{escape(t.locale)}" dir="{"rtl" if t.locale == "fa" else "ltr"}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">{meta}
<title>{escape(title)}</title><link rel="stylesheet" href="/s/_/page.css?v={escape(__version__)}"></head>
<body><main><header class="top"><h1>{escape(title)}</h1>{lang}</header>{body}</main>
<script src="/s/_/page.js?v={escape(__version__)}"></script></body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def _choice(kind: str, plan_id: int) -> str:
    return f"{'a' if kind == 'account' else 's'}:{plan_id}"


def _parse_choice(plan: str, plan_id: int | None) -> tuple[str, int] | None:
    """The form's plan: "s:12" (a service plan) or "a:3" (an admin account plan); a bare
    plan_id from an older page is a service plan."""
    if plan:
        prefix, _, number = plan.partition(":")
        if prefix in {"s", "a"} and number.isdigit() and 0 < int(number) < 2**31:
            return ("account" if prefix == "a" else "service"), int(number)
        return None
    return ("service", plan_id) if plan_id else None


async def _chosen_title(db: DB, kind: str, plan_id: int) -> str:
    from paurubot.db.models import AccountPlan, Plan

    row: AccountPlan | Plan | None = (
        await db.get(AccountPlan, plan_id) if kind == "account" else await db.get(Plan, plan_id)
    )
    return str(row.title) if row is not None else "—"


def _service_option(t: Texts, plan, checked: bool) -> str:  # type: ignore[no-untyped-def]
    specs = t("plan_specs", days=plan.days or t("unlimited"), gb=plan.data_gb or t("unlimited"))
    return _option(t, _choice("service", plan.id), plan.title, specs, plan.price, checked)


def _account_option(t: Texts, plan, checked: bool) -> str:  # type: ignore[no-untyped-def]
    specs = t(
        "guest_account_specs",
        gb=plan.data_gb or t("unlimited"),
        users=plan.max_users or t("unlimited"),
        days=plan.days or t("unlimited"),
    )
    return _option(t, _choice("account", plan.id), plan.title, specs, plan.price, checked)


def _option(t: Texts, value: str, title: str, specs: str, price: int, checked: bool) -> str:
    return (
        f'<label class="opt"><input type="radio" name="plan" value="{escape(value)}" required'
        f'{" checked" if checked else ""}><span class="what"><b>{escape(title)}</b>'
        f"<small>{escape(specs)}</small></span>"
        f'<span class="price">{escape(t("guest_price", price=money(price)))}</span></label>'
    )


async def _store(db: DB, request: Request, shop: Shop, t: Texts, error: str = "") -> HTMLResponse:
    title = shop.title or "PauruBot"
    intent_token = _form_identity(request, shop.id)
    notice = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    intent = await db.get(GuestCheckoutIntent, guest_form.digest(intent_token))
    if intent is not None:
        customer = await db.get(Customer, intent.customer_id) if intent.customer_id else None
        order = await db.get(Order, intent.order_id) if intent.order_id else None
        new_link = f"/buy/{shop.id}?lang={escape(t.locale)}&amp;new=1"
        if guest_checkout.owns(customer, order, shop.id):
            assert customer is not None and order is not None
            title_chosen = await _chosen_title(db, intent.kind, intent.plan_id)
            latest = (await guest.status(db, customer))["payment"]
            # Never invite a second purchase while the first one's outcome is uncertain.
            change = (
                ""
                if latest is not None and saved_checkout.uncertain(latest)
                else f'<a class="btn ghost" href="{new_link}">{escape(t("guest_change_plan"))}</a>'
                f'<p class="note">{escape(t("guest_change_plan_hint"))}</p>'
            )
            body = f'''{notice}<section class="card"><p class="name">{escape(t("guest_waiting"))}</p>
<div class="facts"><div>{escape(t("guest_your_plan"))}<b>{escape(title_chosen)}</b></div>
<div>{escape(t("guest_your_method"))}<b>{escape(t(f"btn_pay_{intent.method}"))}</b></div>
<div>{escape(t("guest_to_pay"))}<b>{escape(t("guest_price", price=money(order.amount)))}</b></div></div>
<form method="post" action="/buy/{shop.id}?lang={escape(t.locale)}">
<input type="hidden" name="intent" value="{escape(intent_token)}">
<input type="hidden" name="plan" value="{escape(_choice(intent.kind, intent.plan_id))}">
<input type="hidden" name="method" value="{escape(intent.method)}">
<button type="submit" class="btn primary">{escape(t("guest_go_pay"))}</button></form>
{change}</section>
<p class="note"><a href="/buy/t/{escape(str(customer.guest_token))}">{escape(t("guest_keep_link"))}</a></p>'''
        else:
            body = f'<section class="card"><p>{escape(_intent_notice(t, "payment_intent_closed"))}</p><a href="{new_link}">{escape(t("guest_buy_again"))}</a></section>'
        page = _page(t, title, body, status=400 if error else 200, switch=f"/buy/{shop.id}")
        return _with_intent(page, shop.id, intent_token)
    plans = await guest.plans(db, shop.id)
    accounts = await guest.account_plans(db, shop.id)
    methods = await guest.methods(db, shop.id)
    if not (plans or accounts) or not methods:
        return _page(t, title, f'<section class="card"><p>{escape(t("guest_closed"))}</p></section>')
    options = "".join(_service_option(t, p, i == 0) for i, p in enumerate(plans))
    account_options = "".join(_account_option(t, p, not plans and i == 0) for i, p in enumerate(accounts))
    if accounts:
        groups = (
            (f'<h3 class="group">{escape(t("guest_group_services"))}</h3>{options}' if plans else "")
            + f'<h3 class="group">{escape(t("guest_group_accounts"))}</h3>'
            + f'<p class="hint">{escape(t("guest_accounts_hint"))}</p>{account_options}'
        )
    else:
        groups = options
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
<input type="hidden" name="intent" value="{escape(intent_token)}">
<section class="card"><h2><span class="step">1</span>{escape(t("guest_pick_plan"))}</h2>{groups}</section>
<section class="card"><h2><span class="step">2</span>{escape(t("guest_pick_method"))}</h2>{ways}
<button type="submit" class="btn primary">{escape(t("guest_pay"))}</button></section>
</form>
<p class="note">{escape(t("guest_how"))}</p>{bot}"""
    page = _page(t, title, body, status=400 if error else 200, switch=f"/buy/{shop.id}")
    return _with_intent(page, shop.id, intent_token)


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
    method: Annotated[str, Form(max_length=32)],
    intent: Annotated[str, Form(max_length=160)] = "",
    plan: Annotated[str, Form(max_length=24)] = "",
    plan_id: Annotated[int | None, Form(gt=0)] = None,
) -> Response:
    shop = await guest.shop_for(db, shop_id)
    if shop is None:
        return _not_found()
    t = await texts_for(db, shop.id, _locale(request, shop))
    cookie_name, _ = _intent_cookie(shop.id)
    cookie = request.cookies.get(cookie_name, "")
    if not guest_form.matches(intent, cookie, shop.id):
        return _page(
            t,
            shop.title or "PauruBot",
            f'<section class="card"><p>{escape(_intent_notice(t, "payment_intent_invalid"))}</p></section>',
            status=403,
        )
    allowed, _ = await kv.hit(f"guest:start:{guest.visitor_key(ip)}", STARTS_PER_IP, STARTS_WINDOW)
    if allowed:  # many addresses at once still cannot flood the shop with half-made purchases
        allowed, _ = await kv.hit(f"guest:start:shop:{shop.id}", STARTS_PER_SHOP_HOUR, 3600)
    if not allowed:
        return await _store(db, request, shop, t, t("guest_too_many"))
    choice = _parse_choice(plan, plan_id)
    if choice is None:
        return await _store(db, request, shop, t, t("guest_plan_unavailable"))
    try:
        token = await guest_checkout.start(shop.id, choice[1], method, t.locale, intent, choice[0])
    except guest.GuestError as exc:
        return await _store(db, request, shop, t, _intent_notice(t, exc.key))
    response = RedirectResponse(f"/buy/t/{token}?go=1", status_code=303)
    settings = get_settings()
    response.set_cookie(
        GUEST_COOKIE,
        token,
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
    here = f"/buy/t/{token}"  # this page, for the language link
    state = await guest.status(db, customer)
    order, payment, service = state["order"], state["payment"], state["service"]
    keep = f'<p class="note">{escape(t("guest_keep_link"))}</p>'
    again = f'<a class="btn ghost" href="/buy/{shop.id}?lang={escape(t.locale)}&amp;new=1">{escape(t("guest_buy_again"))}</a>'

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
        return _page(t, title, body, switch=here)

    account = state["account"]
    if account is not None and order is not None and order.status == OrderStatus.COMPLETED:
        return _page(t, title, _account_ready(t, shop, token, account) + keep, switch=here)

    if payment is not None and payment.status in OPEN:
        if saved_checkout.uncertain(payment):
            # No new-purchase button, stale redirect, or provider I/O on an ambiguous attempt.
            body = f'<section class="card"><p class="name">{escape(t("guest_review"))}</p></section>{keep}'
            return _page(t, title, body, refresh="10", switch=here)
        if "order_attempt_v1" in (payment.meta or {}):
            # Never resume an invoice whose recorded order price/identity changed.
            saved = (payment.meta or {}).get("checkout_intent_v1")
            expected = (
                {
                    "provider": payment.provider,
                    "order_id": order.id,
                    "order_amount": order.amount,
                    "currency": "IRT",
                }
                if order is not None
                else None
            )
            same_request = isinstance(saved, dict) and saved.get("request") == expected
            snapshot = saved_checkout.saved_payment(payment, utcnow(), order) if same_request else None
            url = str((snapshot or {}).get("redirect_url") or "")
        else:
            # Historical guests are status-only; preserve their existing safe link,
            # but do not adopt legacy rows into the new payment-creation protocol.
            url = saved_checkout.safe_https((payment.meta or {}).get("redirect_url")) or ""
            if payment.expires_at and payment.expires_at <= utcnow():
                url = ""
        go = request.query_params.get("go") == "1" and bool(url)
        button = f'<a class="btn primary" href="{escape(url)}">{escape(t("guest_go_pay"))}</a>' if url else ""
        change = (
            f'<a class="btn ghost" href="/buy/{shop.id}?lang={escape(t.locale)}&amp;new=1">'
            f"{escape(t('guest_change_plan'))}</a>"
            f'<p class="note">{escape(t("guest_change_plan_hint"))}</p>'
        )
        body = f"""<section class="card"><p class="name">{escape(t("guest_waiting"))}</p>
<p class="status">{escape(t("guest_amount", amount=money(_to_pay(payment))))}</p>{button}{change}</section>{keep}"""
        return _page(t, title, body, refresh=f"0;url={url}" if go else "10", switch=here)

    paid = payment is not None and payment.status == PaymentStatus.PAID
    if paid or (order is not None and order.status in (OrderStatus.PAID, OrderStatus.PROVISIONING)):
        body = f'<section class="card"><p class="name">{escape(t("guest_building"))}</p></section>{keep}'
        return _page(t, title, body, refresh="5", switch=here)

    reason = (
        "guest_review" if payment is not None and payment.status == PaymentStatus.DISPUTED else "guest_failed"
    )
    body = f'<section class="card"><p class="name">{escape(t(reason))}</p>{again}</section>{keep}'
    return _page(t, title, body, switch=here)


def _account_ready(t: Texts, shop: Shop, token: str, account) -> str:  # type: ignore[no-untyped-def]
    """A bought admin account: its quota, users and end, and the panel login to copy."""
    from paurubot.core import panel_accounts

    url = panel_accounts.login_url(account)
    password = panel_accounts.password_of(account)
    volume = size(account.data_limit) if account.data_limit else t("unlimited")
    ends = jdate(account.expire_at) if account.expire_at else t("unlimited")
    copied = escape(t("sub_page_copied"))
    creds = "".join(
        f'<div class="cred"><small>{escape(label)}</small><code dir="ltr">{escape(value)}</code>'
        f'<button type="button" class="btn ghost sm" data-copy="{escape(value)}" data-done="{copied}">'
        f"{escape(t('sub_page_copy'))}</button></div>"
        for label, value in (
            (t("guest_account_url"), url),
            (t("guest_account_username"), account.username),
            (t("guest_account_password"), password),
        )
        if value
    )
    open_panel = (
        f'<a class="btn primary" href="{escape(url)}">{escape(t("guest_account_open"))}</a>' if url else ""
    )
    claim = (
        f'<a class="btn primary" href="https://t.me/{escape(shop.bot_username)}?start=claim_{escape(token)}">'
        f'{escape(t("guest_add_to_telegram"))}</a><p class="note">{escape(t("guest_account_claim_hint"))}</p>'
        if shop.bot_username
        else ""
    )
    again = (
        f'<a class="btn ghost" href="/buy/{shop.id}?lang={escape(t.locale)}&amp;new=1">'
        f"{escape(t('guest_buy_again'))}</a>"
    )
    return f"""<section class="card"><p class="name">{escape(t("guest_account_ready"))}</p>
<div class="facts"><div>{escape(t("guest_volume"))}<b dir="ltr">{escape(volume)}</b></div>
<div>{escape(t("guest_account_users"))}<b>{escape(str(account.max_users or t("unlimited")))}</b></div>
<div>{escape(t("sub_page_expiry"))}<b>{escape(ends)}</b></div></div></section>
<section class="card">{creds}{open_panel}<p class="note">{escape(t("guest_account_secret"))}</p></section>
<section class="card">{claim}{again}</section>"""


def _to_pay(payment) -> int:  # type: ignore[no-untyped-def]
    from paurubot.payments.engine import amount_to_pay

    return amount_to_pay(payment)
