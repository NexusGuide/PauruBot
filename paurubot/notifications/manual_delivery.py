"""Deliver text/file bundles without exposing encrypted envelope internals."""

from __future__ import annotations

from aiogram.types import BufferedInputFile

from paurubot.core import manual_stock
from paurubot.core.formatting import esc
from paurubot.db.models import Customer, StockItem
from paurubot.notifications import telegram


async def send(customer: Customer, item: StockItem, title: str) -> None:
    for index, part in enumerate(manual_stock.parts(item), 1):
        if part.data is not None or part.as_file or len(part.text.encode("utf-16-le")) > 6500:
            await telegram.send_document(
                customer.telegram_id,
                BufferedInputFile(part.content(), part.filename or f"config-{item.id}-{index}.txt"),
                caption=title,
                shop_id=customer.shop_id,
            )
        else:
            await telegram.send_text(
                customer.telegram_id,
                title + "\n\n<code>" + esc(part.text) + "</code>",
                shop_id=customer.shop_id,
            )
        if part.subscription_url:
            await telegram.send_text(
                customer.telegram_id, esc(part.subscription_url), shop_id=customer.shop_id
            )
