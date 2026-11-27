"""Text rendering shared by bot handlers and the notification worker."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import ssl
import time
from collections.abc import Mapping
from typing import Any

import httpx
import qrcode
from qrcode.exceptions import DataOverflowError

from paurubot.core.formatting import days_left, esc, jdate, money, size
from paurubot.db.models import Order, Plan, Service
from paurubot.i18n import Texts, render as render_template

log = logging.getLogger(__name__)
MAX_QR_BG_BYTES = 2 * 1024 * 1024
MAX_QR_BG_DIM = 4096
MAX_QR_BG_PIXELS = 16_000_000
_QR_BG_CACHE_TTL = 600.0
_QR_BG_CACHE_MAX = 8
_qr_bg_cache: dict[str, tuple[float, bytes]] = {}


def service_card(t: Texts, service: Service, link: str | None = None) -> str:
    if service.data_limit:
        remaining = max(0, service.data_limit - service.used_bytes)
        volume = t(
            "volume_limited",
            used=size(service.used_bytes),
            total=size(service.data_limit),
            remaining=size(remaining),
        )
    else:
        volume = t("volume_unlimited", used=size(service.used_bytes))
    if service.expire_at is None:
        expiry = t("expiry_unlimited")
    else:
        expiry = t("expiry_date", date=jdate(service.expire_at), days=days_left(service.expire_at))
    from paurubot.panels.registry import delivery_of

    kind = delivery_of(service.panel.kind if service.panel else None)
    key = {"wireguard": "service_card_wireguard", "account": "service_card_account"}.get(kind, "service_card")
    card = t(
        key,
        password="-"
        if service.panel and service.panel.national_mode
        else esc((service.panel_data or {}).get("password") or "-"),
        label=esc(service.label or service.panel_username),
        username=esc(service.panel_username),
        status=t(f"status_{service.status.value}"),
        location=esc(
            service.panel.location.title
            if service.panel and service.panel.location
            else (service.panel.name if service.panel else "-")
        ),
        volume=volume,
        expiry=expiry,
        sub="-"
        if service.panel and service.panel.national_mode
        else esc(link or service.subscription_url or "-"),
    )
    if service.panel and service.panel.national_mode:
        card += "\n\n" + t("national_service_notice")
    return card


def plan_specs(t: Texts, plan: Plan) -> str:
    days = t("unlimited") if not plan.days else str(plan.days)
    gb = t("unlimited") if not plan.data_gb else str(plan.data_gb)
    return t("plan_specs", days=days, gb=gb)


def checkout_text(
    t: Texts, order: Order, *, plan: Plan | None, service: Service | None, balance: int, code: str | None
) -> str:
    lines = [t(f"checkout_header_{order.kind.value}")]
    if order.params.get("national"):
        lines.append(t("national_checkout_notice"))
    if plan is not None:
        lines.append(t("checkout_plan", title=esc(plan.title)))
        lines.append(plan_specs(t, plan))
        if plan.on_hold:
            lines.append(t("checkout_on_hold"))
    custom = order.params.get("custom")
    if custom:
        lines.append(t("checkout_custom"))
        lines.append(t("plan_specs", days=custom.get("days"), gb=custom.get("gb")))
    if service is not None:
        lines.append(t("checkout_service", name=esc(service.label or service.panel_username)))
    if order.params.get("extra_gb"):
        lines.append(t("checkout_extra_gb", value=order.params["extra_gb"]))
    if order.params.get("extra_days"):
        lines.append(t("checkout_extra_days", value=order.params["extra_days"]))
    if order.params.get("credit"):
        lines.append(t("checkout_credit", amount=money(order.params["credit"])))
    if order.params.get("label") and service is None:
        lines.append(t("checkout_label", label=esc(order.params["label"])))
    lines.append(t("checkout_price", price=money(order.base_price)))
    if order.reseller_discount:
        key = "checkout_tier_discount" if order.params.get("tier") else "checkout_reseller_discount"
        lines.append(t(key, amount=money(order.reseller_discount)))
    if order.code_discount:
        lines.append(t("checkout_code_discount", amount=money(order.code_discount), code=esc(code or "")))
    lines.append(t("checkout_total", amount=money(order.amount)))
    lines.append(t("checkout_balance", balance=money(balance)))
    if balance < order.amount:
        lines.append(t("checkout_need_more", amount=money(order.amount - balance)))
    return "\n".join(lines)


def qr_png(data: str) -> bytes | None:
    """A QR code as PNG, or None when the text is too long for one (a long WireGuard file)."""
    try:
        img = qrcode.make(data, box_size=8, border=2)
    except DataOverflowError:
        return None
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_base64(data: str) -> str:
    png = qr_png(data) if data else None
    return base64.b64encode(png).decode() if png else ""


def qr_with_background(data: str, bg_bytes: bytes) -> bytes | None:
    """Composite a high-contrast QR tile centered on a background image, or None on invalid input."""
    from PIL import Image, ImageDraw, ImageOps

    try:
        with Image.open(io.BytesIO(bg_bytes)) as opened:
            width, height = opened.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_QR_BG_DIM
                or height > MAX_QR_BG_DIM
                or width * height > MAX_QR_BG_PIXELS
            ):
                return None
            canvas = ImageOps.exif_transpose(opened).convert("RGB")
    except Exception:
        return None
    max_side = 1080
    if max(canvas.size) > max_side:
        canvas.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    cw, ch = canvas.size
    tile_size = max(220, min(int(min(cw, ch) * 0.62), min(cw, ch) - 24))
    pad = max(14, tile_size // 16)
    qr_size = max(120, tile_size - 2 * pad)
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    code.add_data(data)
    try:
        code.make(fit=True)
    except DataOverflowError:
        return None
    qr_img = code.make_image(fill_color="#111827", back_color="#FFFFFF").get_image().convert("RGB")
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)
    left = (cw - tile_size) // 2
    top = (ch - tile_size) // 2
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (left, top, left + tile_size, top + tile_size),
        radius=max(16, tile_size // 18),
        fill="#FFFFFF",
    )
    canvas.paste(qr_img, (left + pad, top + pad))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def fetch_qr_background(url: str) -> bytes | None:
    """Fetch a custom QR background over HTTPS with SSRF validation and strict size bounds."""
    clean = url.strip()
    if not clean.startswith("https://"):
        return None
    now = time.monotonic()
    cached = _qr_bg_cache.get(clean)
    if cached and cached[0] > now:
        return cached[1]
    from paurubot.security.http import ValidatedTransport

    try:
        transport = ValidatedTransport(clean, context=ssl.create_default_context(), allow_private=False)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, timeout=10.0
        ) as client:
            async with client.stream("GET", clean) as response:
                if response.status_code != 200:
                    return None
                ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if ctype and not ctype.startswith("image/"):
                    return None
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_QR_BG_BYTES:
                        return None
                raw = bytes(buf)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        log.warning("QR background fetch failed: %s", exc)
        return None
    if not raw:
        return None
    if len(_qr_bg_cache) >= _QR_BG_CACHE_MAX:
        oldest = min(_qr_bg_cache, key=lambda key: _qr_bg_cache[key][0])
        _qr_bg_cache.pop(oldest, None)
    _qr_bg_cache[clean] = (now + _QR_BG_CACHE_TTL, raw)
    return raw


def render_qr_caption(t: Texts, service: Any, link: str, cfg: Mapping[str, Any]) -> str:
    """Render the shop's custom QR caption (with safe placeholders) or the default localized caption."""
    tail = f"\n<code>{esc(link)}</code>" if cfg.get("qr_show_link") else ""
    custom = str(cfg.get("qr_caption") or "").strip()
    if not custom:
        return (t("qr_caption") + tail)[:1024]
    name = esc(str(getattr(service, "label", None) or getattr(service, "panel_username", "") or ""))
    username = esc(str(getattr(service, "panel_username", "") or ""))
    shop = str(t.context.get("shop") or "")
    rendered = render_template(
        esc(custom),
        {"name": name, "username": username, "shop": shop, "link": f"<code>{esc(link)}</code>"},
    )
    if tail and "{link}" not in custom:
        rendered += tail
    return rendered[:1024]


async def build_qr_photo(
    t: Texts, service: Any, link: str, cfg: Mapping[str, Any]
) -> tuple[bytes, str] | None:
    """Build the QR PNG (composited on qr_background_url when configured) and its caption."""
    png: bytes | None = None
    bg_url = str(cfg.get("qr_background_url") or "").strip()
    if bg_url:
        bg_bytes = await fetch_qr_background(bg_url)
        if bg_bytes:
            png = await asyncio.to_thread(qr_with_background, link, bg_bytes)
    if png is None:
        png = qr_png(link)
    if png is None:
        return None
    return png, render_qr_caption(t, service, link, cfg)
