"""Raw bounded upload and authenticated attachment proxy. Auth before any provider I/O."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Buffer
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.api.webapp_auth import WebApp
from paurubot.core import ticket_media, tickets
from paurubot.db.models import Ticket, TicketMessage, TicketStatus
from paurubot.worker import queue

router = APIRouter(tags=["webapp"])


async def media_response(
    session: AsyncSession, ticket: Ticket, message_id: int, *, internal: bool = False
) -> Response:
    row = await session.scalar(
        select(TicketMessage).where(TicketMessage.id == message_id, TicketMessage.ticket_id == ticket.id)
    )
    if row is None or (row.is_internal and not internal):
        raise HTTPException(status_code=404, detail="ticket_file_not_found")
    if row.attachment_expired:
        raise HTTPException(status_code=410, detail="ticket_file_expired")
    data = await session.scalar(select(TicketMessage.attachment_data).where(TicketMessage.id == row.id))
    if data is not None:
        mime, name = row.attachment_mime or "application/octet-stream", row.attachment_name or "attachment"
    elif row.attachment_file_id:
        from paurubot.notifications import telegram

        # Legacy Telegram files are never exposed as provider URLs/tokens.
        bot = await telegram.bot_for(ticket.shop_id)
        if bot is None:
            raise HTTPException(status_code=503, detail="ticket_file_unavailable")

        class BoundedBuffer(io.BytesIO):
            def write(self, data: Buffer, /) -> int:
                if self.tell() + memoryview(data).nbytes > ticket_media.MAX_BYTES:
                    raise HTTPException(status_code=413, detail="ticket_file_too_large")
                return super().write(data)

        try:
            info = await bot.get_file(row.attachment_file_id)
            if not info.file_path or (info.file_size or 0) > ticket_media.MAX_BYTES:
                raise HTTPException(status_code=413, detail="ticket_file_too_large")
            target = BoundedBuffer()
            await bot.download_file(info.file_path, destination=target, timeout=15)
            raw = target.getvalue()
            mime = (
                "image/png"
                if raw.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg"
                if raw.startswith(b"\xff\xd8\xff")
                else "text/plain"
            )
            data, mime, name = ticket_media.validate(raw, mime)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="ticket_file_unavailable") from exc
    else:
        raise HTTPException(status_code=404, detail="ticket_file_not_found")
    return Response(
        data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


@router.get("/tickets/{ticket_id}/messages/{message_id}/attachment")
async def attachment(ticket_id: int, message_id: int, ctx: WebApp) -> Response:
    from paurubot.api.routes.webapp import _own_ticket

    ticket = await _own_ticket(ctx, ticket_id)
    return await media_response(ctx.session, ticket, message_id)


@router.post("/tickets/{ticket_id}/attachments")
async def upload(
    ticket_id: int,
    request: Request,
    ctx: WebApp,
    request_key: Annotated[
        str, Header(alias="X-Ticket-Request-Key", min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    ],
) -> dict:
    from paurubot.api.routes.webapp import _own_ticket, _ticket_view

    # Reject foreign ticket before reading bytes, then stream under a hard bound.
    await _own_ticket(ctx, ticket_id)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > ticket_media.MAX_BYTES:
            raise HTTPException(status_code=413, detail="ticket_file_too_large")
        data.extend(chunk)
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    digest = ticket_media.fingerprint({"file": hashlib.sha256(data).hexdigest(), "mime": mime})
    # Common lock order with new-ticket operations. Ticket close/reply only take ticket lock.
    await ctx.session.refresh(ctx.customer, ["id"], with_for_update=True)
    ticket = await _own_ticket(ctx, ticket_id, lock=True)
    previous = await ticket_media.replayed(ctx.session, ticket, ctx.customer.id, request_key, digest)
    if previous is not None:
        return {"ticket": _ticket_view(ticket), "message_id": previous.id}
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=409, detail="ticket_closed")
    clean, mime, name = ticket_media.validate(bytes(data), mime)
    await ticket_media.quota(ctx.session, ticket, len(clean))
    message = await tickets.add_message(
        ctx.session,
        ticket,
        author_type="customer",
        author_id=ctx.customer.id,
        text=None,
        attachment_kind="photo" if mime.startswith("image/") else "document",
    )
    message.request_key, message.request_hash = request_key, digest
    message.attachment_data, message.attachment_size = clean, len(clean)
    message.attachment_mime, message.attachment_name = mime, name
    await queue.enqueue(
        ctx.session,
        "notify",
        "ticket_new_message",
        {"ticket_id": ticket.id, "message_id": message.id},
        shop_id=ctx.shop.id,
    )
    return {"ticket": _ticket_view(ticket), "message_id": message.id}
