"""Branded subscription endpoint /s/<token> (SVC-08)."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from paurubot import kv
from paurubot.api.deps import DB
from paurubot.bot.views import qr_base64
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import days_left, jdate, size
from paurubot.db.models import Customer, Panel, Service, ServiceStatus, Shop
from paurubot.i18n import texts_for
from paurubot.panels import PanelError, driver_for
from paurubot.provisioning import subscription

log = logging.getLogger(__name__)
router = APIRouter(tags=["subscription"])

RATE_LIMIT = 30
RATE_WINDOW = 60

PAGE_CSS = """
:root{--bg:#f3f5f9;--card:#fff;--ink:#16202e;--muted:#5d6b7e;--line:#dfe4ec;--accent:#2563eb;
--accent-ink:#fff;--good:#15803d;--warn:#b45309;--bad:#b91c1c;--bar:#e6eaf1}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--card:#18202b;--ink:#e8edf4;--muted:#9aa7b8;
--line:#2a3442;--accent:#5b8def;--accent-ink:#0b1220;--bar:#2a3442}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Vazirmatn,Tahoma,system-ui,sans-serif}
main{max-width:460px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:15px;font-weight:600;color:var(--muted);margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.name{font-size:20px;font-weight:700;margin:0 0 4px;word-break:break-all}
.status{font-size:14px;color:var(--muted)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.facts div{font-size:13px;color:var(--muted)}
.facts b{display:block;font-size:16px;color:var(--ink)}
.bar{display:block;width:100%;height:8px;margin-top:14px;border:0;border-radius:4px;background:var(--bar);
appearance:none;-webkit-appearance:none;color:var(--accent);overflow:hidden}
.bar::-webkit-progress-bar{background:var(--bar);border-radius:4px}
.bar::-webkit-progress-value{background:currentColor;border-radius:4px}
.bar::-moz-progress-bar{background:currentColor;border-radius:4px}
.bar.hi{color:var(--warn)}.bar.full{color:var(--bad)}
.qr{display:block;width:220px;max-width:100%;margin:4px auto 14px;border-radius:10px;background:#fff;padding:8px}
.link{direction:ltr;text-align:left;font:13px/1.5 ui-monospace,monospace;background:var(--bg);
border:1px solid var(--line);border-radius:10px;padding:10px;word-break:break-all;margin-bottom:12px}
.btn{display:block;width:100%;text-align:center;padding:12px;border-radius:10px;border:0;font:inherit;
font-weight:600;text-decoration:none;cursor:pointer;margin-top:8px}
.primary{background:var(--accent);color:var(--accent-ink)}
.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
.apps{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.apps .btn{margin:0}
h2{font-size:15px;margin:0 0 10px}
details summary{cursor:pointer;color:var(--muted);font-size:14px}
details .link{margin:8px 0 0}
.note{font-size:13px;color:var(--muted);text-align:center}
.opt{display:flex;gap:10px;align-items:flex-start;padding:12px;border:1px solid var(--line);border-radius:10px;
margin-bottom:8px;cursor:pointer}
.opt input{margin-top:5px;accent-color:var(--accent)}
.opt small{display:block;color:var(--muted);font-size:13px}
.opt:has(input:checked){border-color:var(--accent)}
.price{display:block;font-weight:700;margin-top:2px}
.err{background:var(--card);border:1px solid var(--bad);color:var(--bad);border-radius:10px;padding:10px 12px;
font-weight:600}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""

PAGE_JS = """
document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
var v=b.getAttribute('data-copy');var done=b.getAttribute('data-done');var label=b.textContent;
function ok(){b.textContent=done;setTimeout(function(){b.textContent=label},1600)}
if(navigator.clipboard){navigator.clipboard.writeText(v).then(ok)}else{var t=document.createElement('textarea');
t.value=v;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();ok()}})});
"""


@router.get("/s/_/page.css", include_in_schema=False)
async def page_css() -> Response:
    return Response(PAGE_CSS, media_type="text/css", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/s/_/page.js", include_in_schema=False)
async def page_js() -> Response:
    return Response(
        PAGE_JS, media_type="application/javascript", headers={"Cache-Control": "public, max-age=86400"}
    )


def _not_found() -> Response:
    return Response("not found", status_code=404, media_type="text/plain")


@router.get("/s/{token}", include_in_schema=False)
async def subscription_endpoint(token: str, request: Request, db: DB) -> Response:
    if not subscription.TOKEN_RE.match(token):
        return _not_found()
    allowed, _ = await kv.hit(f"sub:{token}", RATE_LIMIT, RATE_WINDOW)
    if not allowed:
        return Response("slow down", status_code=429, media_type="text/plain", headers={"Retry-After": "60"})
    service = await db.scalar(select(Service).where(Service.sub_token == token))
    if service is None or service.status == ServiceStatus.DELETED:
        return _not_found()
    shop = await db.get(Shop, service.shop_id)
    panel = await db.get(Panel, service.panel_id)
    if shop is None or panel is None:
        return _not_found()
    title = shop.title or "PauruBot"
    raw = request.query_params.get("download") == "1"
    if not raw and subscription.wants_page(
        request.headers.get("user-agent", ""), request.headers.get("accept", "")
    ):
        return await _page(db, request, service, shop, panel)
    try:
        body = await (await driver_for(panel)).fetch_subscription(
            service.subscription_url, request.headers.get("user-agent") or "v2rayNG/1.8.0"
        )
    except PanelError:
        body = None
    if body is None:
        return Response("subscription unavailable", status_code=502, media_type="text/plain")
    headers = dict(body.headers)
    headers["profile-title"] = subscription.profile_title(title)
    headers.setdefault("profile-update-interval", "12")
    headers["Cache-Control"] = "no-store"
    return Response(body.content, media_type=body.content_type.split(";")[0] or "text/plain", headers=headers)


async def _page(db: DB, request: Request, service: Service, shop: Shop, panel: Panel) -> Response:
    customer = await db.get(Customer, service.customer_id)
    t = await texts_for(db, shop.id, (customer.locale if customer else None) or shop.default_locale)
    cfg = await shop_settings.load(db, shop.id)
    link = subscription.branded_url(service) or str(request.url).split("?")[0]
    title = shop.title or "PauruBot"
    rtl = t.locale == "fa"
    if service.data_limit:
        percent = min(100, int(service.used_bytes * 100 / service.data_limit))
        volume = f"{size(service.used_bytes)} / {size(service.data_limit)}"
    else:
        percent, volume = 0, t("sub_page_unlimited_volume", used=size(service.used_bytes))
    if service.expire_at is None:
        expiry = t("sub_page_on_hold") if service.status == ServiceStatus.ON_HOLD else t("unlimited")
        remaining = "-"
    else:
        expiry = jdate(service.expire_at)
        remaining = t("sub_page_days", days=max(0, days_left(service.expire_at) or 0))
    bar_class = "full" if percent >= 100 else "hi" if percent >= 80 else ""
    from paurubot.panels.registry import delivery_of

    kind = delivery_of(panel.kind)
    if kind != "subscription":
        return await _special_page(
            request, t, service, panel, kind, title, rtl, percent, bar_class, volume, remaining, expiry, link
        )
    qr = qr_base64(link)
    image = f'<img class="qr" src="data:image/png;base64,{qr}" alt="{escape(t("qr_caption"))}">' if qr else ""
    apps = "".join(
        f'<a class="btn ghost" href="{escape(app["url"])}">{escape(app["name"])}</a>'
        for app in subscription.app_buttons(cfg, link, title)
    )
    configs = ""
    try:
        links = await (await driver_for(panel)).fetch_links(service.subscription_url)
    except PanelError:
        links = []
    if links:
        items = "".join(f'<div class="link">{escape(item)}</div>' for item in links[:30])
        configs = (
            f'<section class="card"><details><summary>{escape(t("sub_page_configs", count=len(links)))}'
            f"</summary>{items}</details></section>"
        )
    support = str(cfg.get("support_username") or "").lstrip("@")
    support_html = (
        f'<p class="note"><a href="https://t.me/{escape(support)}">{escape(t("sub_page_support"))}</a></p>'
        if support
        else ""
    )
    html = f"""<!doctype html>
<html lang="{escape(t.locale)}" dir="{"rtl" if rtl else "ltr"}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{escape(title)}</title>
<link rel="stylesheet" href="/s/_/page.css"></head>
<body><main>
<h1>{escape(title)}</h1>
<section class="card">
<p class="name">{escape(service.label or service.panel_username)}</p>
<div class="status">{escape(t(f"status_{service.status.value}"))}</div>
<progress class="bar {bar_class}" value="{percent}" max="100">{percent}%</progress>
<div class="facts">
<div>{escape(t("sub_page_volume"))}<b dir="ltr">{escape(volume)}</b></div>
<div>{escape(t("sub_page_remaining"))}<b>{escape(remaining)}</b></div>
<div>{escape(t("sub_page_expiry"))}<b>{escape(expiry)}</b></div>
</div></section>
<section class="card">
{image}
<div class="link">{escape(link)}</div>
<button type="button" class="btn primary" data-copy="{escape(link)}" data-done="{escape(t("sub_page_copied"))}">
{escape(t("sub_page_copy"))}</button>
</section>
<section class="card"><h2>{escape(t("sub_page_import"))}</h2><div class="apps">{apps}</div></section>
{configs}
{support_html}
</main><script src="/s/_/page.js"></script></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


async def _special_page(
    request: Request,
    t: Any,
    service: Service,
    panel: Panel,
    kind: str,
    title: str,
    rtl: bool,
    percent: int,
    bar_class: str,
    volume: str,
    remaining: str,
    expiry: str,
    link: str,
) -> Response:
    """WireGuard: the .conf as a QR and a download; username and password panels: the login."""
    if kind == "wireguard":
        try:
            items = await (await driver_for(panel)).fetch_links(service.subscription_url)
        except PanelError:
            items = []
        if items:
            qr = qr_base64(items[0])
            access = (
                (
                    f'<img class="qr" src="data:image/png;base64,{qr}" alt="{escape(t("qr_caption"))}">'
                    if qr
                    else ""
                )
                + f'<a class="btn primary" href="{escape(link)}?download=1">{escape(t("sub_page_download_conf"))}</a>'
            )
        else:
            access = f'<p class="status">{escape(t("wireguard_unavailable"))}</p>'
    else:
        password = str((service.panel_data or {}).get("password") or "-")
        access = (
            f'<div class="facts"><div>{escape(t("sub_page_username"))}<b dir="ltr">{escape(service.panel_username)}</b></div>'
            f'<div>{escape(t("sub_page_password"))}<b dir="ltr">{escape(password)}</b></div></div>'
        )
    html = f"""<!doctype html>
<html lang="{escape(t.locale)}" dir="{"rtl" if rtl else "ltr"}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{escape(title)}</title>
<link rel="stylesheet" href="/s/_/page.css"></head>
<body><main>
<h1>{escape(title)}</h1>
<section class="card">
<p class="name">{escape(service.label or service.panel_username)}</p>
<div class="status">{escape(t(f"status_{service.status.value}"))}</div>
<progress class="bar {bar_class}" value="{percent}" max="100">{percent}%</progress>
<div class="facts">
<div>{escape(t("sub_page_volume"))}<b dir="ltr">{escape(volume)}</b></div>
<div>{escape(t("sub_page_remaining"))}<b>{escape(remaining)}</b></div>
<div>{escape(t("sub_page_expiry"))}<b>{escape(expiry)}</b></div>
</div></section>
<section class="card">{access}</section>
</main><script src="/s/_/page.js"></script></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
