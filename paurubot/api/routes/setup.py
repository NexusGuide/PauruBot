from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from paurubot.api.deps import DB, request_ip
from paurubot.core import setup
from paurubot.core.shops import is_set_up

router = APIRouter(prefix="/api/setup", tags=["setup"])


class SetupBody(BaseModel):
    token: str = Field(min_length=10, max_length=128)
    shop_title: str = Field(min_length=1, max_length=96)
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=256)
    telegram_id: int | None = Field(default=None, gt=0)


@router.get("/status")
async def status(db: DB) -> dict:
    return {"setup_required": not await is_set_up(db)}


@router.post("/complete")
async def complete(body: SetupBody, request: Request, db: DB) -> dict:
    try:
        shop, owner = await setup.complete(
            db,
            token=body.token,
            shop_title=body.shop_title,
            username=body.username,
            password=body.password,
            telegram_id=body.telegram_id,
            ip=request_ip(request),
        )
    except setup.SetupError as exc:
        code = 409 if str(exc) == "already_set_up" else 403
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {"shop_id": shop.id, "owner_id": owner.id}
