"""Web side of the two-step confirmation for destructive actions (F-22).

Issuing a challenge always works, so the panel can show the typing dialog everywhere. The check is
enforced when the shop turns on ``typed_confirm_enabled``; it answers 428 ``confirmation_required``
when the token or the typed phrase is missing or wrong.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from paurubot.api.deps import DB, StaffContext
from paurubot.core import confirm
from paurubot.core import settings as shop_settings


class ConfirmFields(BaseModel):
    confirm_token: str | None = Field(default=None, max_length=64)
    confirm_text: str | None = Field(default=None, max_length=128)


def actor_key(ctx: StaffContext) -> str:
    return f"staff:{ctx.staff.id}"


async def enforced(db: DB, shop_id: int) -> bool:
    return bool(await shop_settings.get(db, shop_id, "typed_confirm_enabled"))


async def challenge(ctx: StaffContext, action: str, target: str | int, phrase: str) -> dict[str, Any]:
    issued = await confirm.issue(actor_key(ctx), action, target, phrase)
    return {"action": action, **issued.as_dict()}


async def guard(
    db: DB,
    ctx: StaffContext,
    action: str,
    target: str | int,
    phrase: str,
    fields: ConfirmFields | None,
) -> None:
    if not await enforced(db, ctx.shop_id):
        return
    token = fields.confirm_token if fields else None
    typed = fields.confirm_text if fields else None
    if not await confirm.consume(actor_key(ctx), action, target, token, typed, phrase=phrase):
        raise HTTPException(status_code=428, detail="confirmation_required")
