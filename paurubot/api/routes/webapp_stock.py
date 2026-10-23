"""Customer-owned manual deliveries, including binary bundles; no public download URLs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from paurubot.api.deps import DB
from paurubot.api.webapp_auth import WebApp
from paurubot.core import manual_stock, stock
from paurubot.db.models import StockItem

router = APIRouter(prefix="/api/webapp/stock", tags=["webapp"])


async def owned(db: DB, ctx: WebApp, item_id: int) -> StockItem:
    item = await db.scalar(
        select(StockItem).where(
            StockItem.id == item_id, StockItem.customer_id == ctx.customer.id, StockItem.status == stock.SOLD
        )
    )
    if item is None:
        raise HTTPException(404, "not_found")
    return item


@router.get("")
async def inventory(ctx: WebApp, db: DB, offset: int = Query(0, ge=0)) -> dict[str, Any]:
    items = list(
        await db.scalars(
            select(StockItem)
            .where(StockItem.customer_id == ctx.customer.id, StockItem.status == stock.SOLD)
            .order_by(StockItem.id.desc())
            .offset(offset)
            .limit(26)
        )
    )
    return {
        "items": [
            {
                "id": i.id,
                "name": i.name or i.note or f"#{i.id}",
                "parts": i.part_count,
                "sold_at": i.sold_at.isoformat() if i.sold_at else None,
            }
            for i in items[:25]
        ],
        "next_offset": offset + 25 if len(items) > 25 else None,
    }


@router.get("/{item_id}")
async def content(item_id: int, ctx: WebApp, db: DB, response: Response) -> dict[str, Any]:
    item = await owned(db, ctx, item_id)
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": item.id,
        "parts": [
            p.model_dump(exclude={"data"}) | {"file": p.data is not None} for p in manual_stock.parts(item)
        ],
    }


@router.get("/{item_id}/files/{part_id}")
async def download(item_id: int, part_id: int, ctx: WebApp, db: DB) -> Response:
    item = await owned(db, ctx, item_id)
    parts = manual_stock.parts(item)
    if part_id < 0 or part_id >= len(parts):
        raise HTTPException(404, "not_found")
    part = parts[part_id]
    return Response(
        part.content(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(part.filename or f'config-{item.id}.txt')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
