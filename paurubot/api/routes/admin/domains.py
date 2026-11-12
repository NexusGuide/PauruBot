"""Subscription-link domains and their certificates (ClockBot parity B4).

Anyone with settings.read sees the domains and their state; adding, removing, checking and
choosing the links' domain need panels.secrets (the owner), like the panels' certificates.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot import kv
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import domains
from paurubot.core import settings as shop_settings
from paurubot.db.models import ShopDomain

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("settings.read"))]
Owner = Annotated[StaffContext, Depends(require("panels.secrets"))]

CHECK_EVERY = 10  # seconds between two "check now" of one domain

_STATUS_CODES = {
    "domain_invalid": 422,
    "domain_is_main": 422,
    "domain_limit": 422,
    "domain_taken": 409,
    "domain_not_ready": 409,
    "domain_not_found": 404,
}


def _fail(exc: domains.DomainError) -> HTTPException:
    code = str(exc)
    return HTTPException(status_code=_STATUS_CODES.get(code, 422), detail=code)


def domain_out(row: ShopDomain) -> dict[str, Any]:
    return {
        "id": row.id,
        "host": row.host,
        "for_links": row.for_links,
        "status": row.status,
        "error": row.error,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "cert_expires_at": row.cert_expires_at.isoformat() if row.cert_expires_at else None,
        "cert_issuer": row.cert_issuer,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _owned(db: DB, domain_id: int, ctx: StaffContext) -> ShopDomain:
    row = await db.get(ShopDomain, domain_id)
    if row is None or row.shop_id != ctx.shop_id:
        raise HTTPException(status_code=404, detail="domain_not_found")
    return row


@router.get("/domains")
async def list_domains(ctx: Reader, db: DB) -> dict[str, Any]:
    rows = list(
        await db.scalars(select(ShopDomain).where(ShopDomain.shop_id == ctx.shop_id).order_by(ShopDomain.id))
    )
    return {
        "main": domains.main_host(),
        "domains": [domain_out(r) for r in rows],
        "limit": domains.MAX_PER_SHOP,
    }


class DomainBody(BaseModel):
    host: str = Field(min_length=1, max_length=300)


@router.post("/domains")
async def add_domain(body: DomainBody, ctx: Owner, db: DB) -> dict[str, Any]:
    try:
        row = await domains.add(db, ctx.shop_id, body.host)
    except domains.DomainError as exc:
        raise _fail(exc) from exc
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="domain.add", entity="domain", entity_id=row.id
    )
    await db.commit()
    return domain_out(row)


@router.delete("/domains/{domain_id}")
async def remove_domain(domain_id: int, ctx: Owner, db: DB) -> dict[str, Any]:
    row = await _owned(db, domain_id, ctx)
    await db.delete(row)
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="domain.remove", entity="domain", entity_id=domain_id
    )
    await db.commit()
    shop_settings.invalidate(ctx.shop_id)
    return {"ok": True}


@router.post("/domains/{domain_id}/check")
async def check_domain(domain_id: int, ctx: Owner, db: DB) -> dict[str, Any]:
    row = await _owned(db, domain_id, ctx)
    if not await kv.set_once(f"domain:check:{row.id}", CHECK_EVERY):
        raise HTTPException(status_code=429, detail="domain_check_too_soon")
    await domains.check_row(db, row, probe=domains.default_probe())
    await db.commit()
    shop_settings.invalidate(ctx.shop_id)
    return domain_out(row)


class LinksBody(BaseModel):
    domain_id: int | None = None


@router.put("/domains/links")
async def links_domain(body: LinksBody, ctx: Owner, db: DB) -> dict[str, Any]:
    try:
        await domains.use_for_links(db, ctx.shop_id, body.domain_id)
    except domains.DomainError as exc:
        raise _fail(exc) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="domain.links",
        entity="domain",
        entity_id=body.domain_id,
    )
    await db.commit()
    shop_settings.invalidate(ctx.shop_id)
    return {"ok": True}
