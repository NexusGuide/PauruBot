"""Text rendering shared by bot handlers and the notification worker."""

from __future__ import annotations

import base64
import io

import qrcode
from qrcode.exceptions import DataOverflowError

from paurubot.core.formatting import days_left, esc, jdate, money, size
from paurubot.db.models import Order, Plan, Service
from paurubot.i18n import Texts


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
