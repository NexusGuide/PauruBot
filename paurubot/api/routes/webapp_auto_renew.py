"""Customer-owned automatic wallet renewal preferences (F-02), not an order endpoint."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from paurubot.api.webapp_auth import WebApp
from paurubot.audit.log import Actor, record
from paurubot.core import auto_renew, catalog, orders
from paurubot.db.models import OrderKind, Service, ServiceStatus
from paurubot.ledger.accounts import wallet_balance

router = APIRouter()


class AutoRenewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "renew", "extra"]
    plan_id: int = Field(default=0, ge=0, strict=True)
    gb: int = Field(default=0, ge=0, le=10_000, strict=True)
    days: int = Field(default=0, ge=0, le=3650, strict=True)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_wallet: StrictBool


def revision(service: Service) -> str:
    raw = json.dumps(service.auto_renew, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{service.id}:{raw}".encode()).hexdigest()


def _quantity(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def preference_view(service: Service) -> dict[str, Any]:
    found = auto_renew.rule(service)
    state = "off" if service.auto_renew is None else "unknown" if found is None else "on"
    rule: dict[str, Any] | None = None
    if found is not None:
        state = "stopped" if found.get("stopped") else "waiting_balance" if found.get("short") else "on"
        rule = (
            {"mode": "renew", "plan_id": int(found["plan_id"])}
            if found["mode"] == "renew"
            else {"mode": "extra", "gb": _quantity(found.get("gb")), "days": _quantity(found.get("days"))}
        )
    return {
        "service_id": service.id,
        "revision": revision(service),
        "status": state,
        "rule": rule,
        "reason": str(found.get("stopped"))[:48] if found and found.get("stopped") else None,
    }


async def _owned(ctx: WebApp, service_id: int, *, lock: bool = False) -> Service:
    query = select(Service).where(
        Service.id == service_id,
        Service.customer_id == ctx.customer.id,
        Service.shop_id == ctx.shop.id,
        Service.status != ServiceStatus.DELETED,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    service = await ctx.session.scalar(query)
    if service is None:
        raise HTTPException(status_code=404, detail="service_not_found")
    return service


async def _view(ctx: WebApp, service: Service) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    gb_options: list[dict[str, int]] = []
    day_options: list[dict[str, int]] = []
    eligible = not service.is_trial and service.status in auto_renew.ELIGIBLE
    if eligible:
        offered = await catalog.plans(ctx.session, ctx.shop.id, ctx.customer, panel_id=service.panel_id)
        for plan in offered:
            if not plan.renewable or plan.delivery == "stock":
                continue
            try:
                draft = await orders.draft(
                    ctx.session, ctx.customer, OrderKind.RENEW, plan=plan, service=service
                )
            except orders.OrderError:
                continue
            plans.append({"id": plan.id, "title": plan.title, "base_price": draft.base_price})
        for kind, values, output in (
            (OrderKind.EXTRA_VOLUME, auto_renew.GB_OPTIONS, gb_options),
            (OrderKind.EXTRA_DAYS, auto_renew.DAY_OPTIONS, day_options),
        ):
            for value in values:
                try:
                    draft = await orders.draft(
                        ctx.session,
                        ctx.customer,
                        kind,
                        service=service,
                        extra_gb=value if kind == OrderKind.EXTRA_VOLUME else 0,
                        extra_days=value if kind == OrderKind.EXTRA_DAYS else 0,
                    )
                except orders.OrderError:
                    continue
                output.append({"value": value, "base_price": draft.base_price})
    return {
        **preference_view(service),
        "eligible": eligible,
        "plans": plans,
        "gb_options": gb_options,
        "day_options": day_options,
        "balance": await wallet_balance(ctx.session, ctx.shop.id, ctx.customer.id),
        "currency": ctx.shop.currency,
    }


@router.get("/services/{service_id}/auto-renew")
async def get_auto_renew(service_id: int, ctx: WebApp) -> dict[str, Any]:
    return await _view(ctx, await _owned(ctx, service_id))


@router.post("/services/{service_id}/auto-renew")
async def set_auto_renew(service_id: int, body: AutoRenewBody, ctx: WebApp) -> dict[str, Any]:
    if not body.acknowledge_wallet:
        raise HTTPException(status_code=422, detail="wallet_ack_required")
    if (
        (body.mode == "off" and (body.plan_id or body.gb or body.days))
        or (body.mode == "renew" and (not body.plan_id or body.gb or body.days))
        or (body.mode == "extra" and (body.plan_id or not (body.gb or body.days)))
    ):
        raise HTTPException(status_code=422, detail="auto_rule_invalid")
    service = await _owned(ctx, service_id, lock=True)
    if revision(service) != body.expected_revision:
        raise HTTPException(status_code=409, detail="auto_rule_changed")
    before = preference_view(service)
    desired: dict[str, Any] | None = (
        None
        if body.mode == "off"
        else {"mode": "renew", "plan_id": body.plan_id}
        if body.mode == "renew"
        else {
            "mode": "extra",
            **({"gb": body.gb} if body.gb else {}),
            **({"days": body.days} if body.days else {}),
        }
    )
    current = auto_renew.rule(service)
    previous = dict(service.auto_renew or {}) if isinstance(service.auto_renew, dict) else {}
    normalized = (
        None
        if current is None
        else {k: current[k] for k in ("mode", "plan_id", "gb", "days") if k in current and current[k] != 0}
    )
    # Repeated active settings preserve last-order/cooldown/short metadata and do not audit again.
    if desired == normalized and before["status"] in {"off", "on", "waiting_balance"}:
        return await _view(ctx, service)
    if body.mode != "off" and (service.is_trial or service.status not in auto_renew.ELIGIBLE):
        raise HTTPException(status_code=422, detail="auto_service_ineligible")
    try:
        async with ctx.session.begin_nested():
            # enable() merges existing extra parts; this API replaces the complete explicit choice.
            # Savepoint restores the previous rule if any validation fails.
            await auto_renew.disable(ctx.session, service)
            if body.mode != "off":
                await auto_renew.enable(
                    ctx.session, service, plan_id=body.plan_id, gb=body.gb, days=body.days
                )
                # A changed enabled rule is not permission to bypass the recent-order cooldown.
                # An explicit restart of a stopped rule clears its failed-order marker, as the bot does.
                markers = {k: previous[k] for k in ("last_at", "last_order_id") if k in previous}
                if previous.get("stopped"):
                    markers.pop("last_order_id", None)
                service.auto_renew = {**(service.auto_renew or {}), **markers}
                await ctx.session.flush()
            await record(
                ctx.session,
                shop_id=ctx.shop.id,
                actor=Actor("customer", ctx.customer.id),
                action="service.auto_renew",
                entity="service",
                entity_id=service.id,
                before=before,
                after=preference_view(service),
            )
    except orders.OrderError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from exc
    result = await _view(ctx, service)
    # Report success only after the preference and its audit have actually committed.
    await ctx.session.commit()
    return result
