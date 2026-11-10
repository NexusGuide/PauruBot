"""Quick links into the bot and the message variables its texts can use (settings pages)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from paurubot.api.deps import DB, StaffContext, require
from paurubot.bot.menu import BUTTONS
from paurubot.core import settings as shop_settings
from paurubot.db.models import Shop
from paurubot.i18n import DEFAULT_LOCALE, catalog, fields

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("settings.read"))]

# Menu items a quick link can open; the language picker and the Mini App button are not sections.
LINK_ITEMS = [item for item in BUTTONS if item not in {"language", "miniapp"}]
MAX_KEYS = 8


@router.get("/quick-links")
async def quick_links(ctx: Reader, db: DB) -> dict[str, Any]:
    """`t.me/<bot>?start=go_<item>` for each section, and whether it opens right now."""
    from paurubot.bot.routers.start import item_shown

    shop = await db.get(Shop, ctx.shop_id)
    cfg = await shop_settings.load(db, ctx.shop_id)
    return {
        "bot_username": shop.bot_username if shop else None,
        "items": [{"item": item, "start": f"go_{item}", "on": item_shown(item, cfg)} for item in LINK_ITEMS],
    }


@router.get("/message-variables")
async def message_variables(ctx: Reader) -> dict[str, Any]:
    """Every `{variable}` the bot's texts use, with the texts that use it."""
    texts = catalog(DEFAULT_LOCALE)
    used: dict[str, list[str]] = {}
    for key in sorted(texts):
        for name in fields(texts[key]):
            used.setdefault(name, []).append(key)
    return {
        "items": [
            {"name": name, "count": len(keys), "keys": keys[:MAX_KEYS]}
            for name, keys in sorted(used.items(), key=lambda pair: (-len(pair[1]), pair[0]))
        ]
    }
