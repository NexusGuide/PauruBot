"""The customer's own discounts, and which of them fit the order at hand (F-33).

A customer could only find out whether a code applied by typing it in at checkout, and the Mini
App never showed the codes made for them (GRW-10) or how far the next loyalty tier was (GRW-02).
This lists what the customer has: the standing percent (reseller or tier), the next tier, and
their private and sticky codes. Given an open order, each code is checked with the same rules
checkout uses and the saving is worked out, so the screen can offer the best one. Public codes
stay secret: only codes that already belong to this customer are listed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from paurubot.api.webapp_auth import WebApp
from paurubot.core import growth, pricing
from paurubot.db.models import DiscountCode, Order, OrderStatus

OPEN = (OrderStatus.PENDING, OrderStatus.AWAITING_PAYMENT)

router = APIRouter()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _tiers(ctx: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tiers = ctx.cfg.get("loyalty_tiers") or []
    if not ctx.cfg.get("loyalty_enabled") or not tiers or ctx.customer.is_reseller:
        return None, None
    spent = int(ctx.customer.total_spent or 0)
    current = growth.tier_for(tiers, spent)
    upcoming = growth.next_tier(tiers, spent)
    return (
        {"title": current["title"], "percent": int(current["percent"])} if current else None,
        {
            "title": upcoming["title"],
            "percent": int(upcoming["percent"]),
            "remaining": int(upcoming["min_spent"]) - spent,
        }
        if upcoming
        else None,
    )


async def _order(ctx: Any, order_id: int | None) -> Order | None:
    if order_id is None:
        return None
    order = await ctx.session.get(Order, order_id)
    if order is None or order.customer_id != ctx.customer.id or order.shop_id != ctx.shop.id:
        raise HTTPException(status_code=404, detail="order_not_found")
    if order.status not in OPEN or order.kind.value.startswith("account_"):
        return None  # nothing left to discount
    return order


async def _code_view(ctx: Any, code: DiscountCode, order: Order | None, *, sticky: bool) -> dict[str, Any]:
    view: dict[str, Any] = {
        "code": code.code,
        "kind": code.kind.value,
        "value": int(code.value),
        "max_discount": int(code.max_discount) if code.max_discount else None,
        "min_amount": int(code.min_amount or 0),
        "expires_at": _iso(code.expires_at),
        "first_purchase_only": bool(code.first_purchase_only),
        "sticky": sticky,
        "fits": None,
        "reason": None,
        "saving": None,
    }
    if order is not None:
        # The price the code would cut: after the standing discount, before any code.
        after = int(order.amount) + int(order.code_discount or 0)
        try:
            await pricing.validate_code(ctx.session, code, ctx.customer, plan_id=order.plan_id, amount=after)
        except pricing.DiscountError as exc:
            view.update(fits=False, reason=exc.key)
        else:
            saving = pricing.code_discount_amount(code, after)
            # A reseller bot's code never cuts below what the reseller pays (as at checkout).
            wholesale = int((order.params or {}).get("wholesale") or 0)
            if wholesale > 0:
                saving = min(saving, max(0, after - wholesale))
            view.update(fits=True, saving=saving)
    return view


@router.get("/discounts")
async def discounts(ctx: WebApp, order_id: int | None = Query(None, ge=1, le=2**63 - 1)) -> dict[str, Any]:
    enabled = bool(ctx.cfg.get("discounts_enabled", True))
    order = await _order(ctx, order_id)
    tier, upcoming = _tiers(ctx)
    if not enabled:
        return {"enabled": False, "tier": tier, "next_tier": upcoming, "codes": []}
    sticky = await pricing.sticky_code(ctx.session, ctx.customer)
    codes = [
        await _code_view(ctx, code, order, sticky=False)
        for code in await pricing.private_codes(ctx.session, ctx.customer)
    ]
    if sticky is not None and sticky.code not in {c["code"] for c in codes}:
        codes.insert(0, await _code_view(ctx, sticky, order, sticky=True))
    if order is not None:
        # The best saving first; codes that do not fit this order after, with the reason.
        codes.sort(key=lambda c: (not c["fits"], -(c["saving"] or 0)))
    return {
        "standing_percent": pricing.reseller_percent(ctx.customer),
        "reseller": bool(ctx.customer.is_reseller),
        "tier": tier,
        "next_tier": upcoming,
        "codes": codes,
    }
