"""Helpers shared by admin routers: ownership checks, paging and serialisation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from paurubot.api.deps import StaffContext
from paurubot.db.models import ResellerProfile, Service


async def owned[M: DeclarativeBase](db: AsyncSession, model: type[M], obj_id: int, ctx: StaffContext) -> M:
    """Load a row of the staff member's shop or answer 404 (never 403, to avoid confirming existence)."""
    row = await db.get(model, obj_id)
    if row is None or getattr(row, "shop_id", None) != ctx.shop_id:
        raise HTTPException(status_code=404, detail="not_found")
    return row


class Page(BaseModel):
    limit: int = Field(default=25, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def page_params(limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0)) -> Page:
    return Page(limit=limit, offset=offset)


async def paginate(db: AsyncSession, stmt: Select[Any], page: Page) -> tuple[list[Any], int]:
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await db.scalars(stmt.limit(page.limit).offset(page.offset))).all()
    return list(rows), int(total or 0)


def plain(obj: Any, fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Enum):
            value = value.value
        out[name] = value
    return out


def sort_params(sort: str | None = Query(None, max_length=32, pattern=r"^-?[a-z_]+$")) -> str | None:
    return sort


def apply_sort(stmt: Select[Any], sort: str | None, columns: dict[str, Any], tiebreak: Any) -> Select[Any]:
    """Order by an allow-listed column (`name` ascending, `-name` descending), newest first by default."""
    if not sort:
        return stmt.order_by(tiebreak.desc())
    descending = sort.startswith("-")
    column = columns.get(sort.lstrip("-"))
    if column is None:
        raise HTTPException(status_code=422, detail="bad_sort")
    ordered = column.desc().nulls_last() if descending else column.asc().nulls_last()
    return stmt.order_by(ordered, tiebreak.desc())


async def reseller_customer_id(db: AsyncSession, ctx: StaffContext) -> int | None:
    """The customer behind a reseller staff account; resellers only ever see that customer's services."""
    if not ctx.scoped:
        return None
    if ctx.staff.reseller_profile_id is None:
        raise HTTPException(status_code=403, detail="forbidden")
    profile = await db.get(ResellerProfile, ctx.staff.reseller_profile_id)
    if profile is None or not profile.is_current:
        raise HTTPException(status_code=403, detail="forbidden")
    return profile.customer_id


async def reseller_profile(db: AsyncSession, ctx: StaffContext) -> ResellerProfile | None:
    if await reseller_customer_id(db, ctx) is None:
        return None
    return await db.get(ResellerProfile, ctx.staff.reseller_profile_id)


async def reseller_scope(db: AsyncSession, ctx: StaffContext) -> list[int] | None:
    """Customers a reseller sees: itself plus its level-2 resellers (TEN-01). None for shop staff."""
    profile = await reseller_profile(db, ctx)
    if profile is None:
        return None
    from paurubot.core.resellers import scope_customer_ids

    return await scope_customer_ids(db, profile)


async def scope_services(db: AsyncSession, stmt: Select[Any], ctx: StaffContext) -> Select[Any]:
    scope = await reseller_scope(db, ctx)
    if scope is not None:
        stmt = stmt.where(Service.customer_id.in_(scope))
    return stmt
