"""Find a service from the subscription link a customer pasted to support (REF-07).

Customers send the link, not their account name. Staff can now paste the shop's branded link
(/s/<token>), the panel's own subscription URL or a long bare token into the search and get the
service and its owner. Matching stays inside the staff member's shop, so another shop's link reads
as unknown. The token is a bearer secret: it is only compared in the query, never logged or stored.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Service

TOKEN = re.compile(r"^[A-Za-z0-9_\-=.~]{12,200}$")
# A bare word this long is never a Telegram username (32 at most), an id or a service label.
BARE_MIN = 33
MAX_INPUT = 512


def link_token(raw: str | None) -> str | None:
    """The token in a subscription URL, or an unmistakable bare token; None for anything else."""
    text = (raw or "").strip()[:MAX_INPUT]
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return None
        segments = [segment for segment in parts.path.split("/") if segment]
        token = segments[-1] if segments else ""
    elif "/" in text:
        return None  # a path without a host is too loose to search by
    else:
        token = text if len(text) >= BARE_MIN else ""
    return token if TOKEN.match(token) else None


def condition(token: str) -> ColumnElement[bool]:
    """Services whose branded token, or panel URL's last path part, is `token`."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(
        Service.sub_token == token,
        Service.subscription_url.like(f"%/{escaped}", escape="\\"),
        Service.subscription_url.like(f"%/{escaped}/", escape="\\"),
        Service.subscription_url.like(f"%/{escaped}?%", escape="\\"),
    )


async def find(session: AsyncSession, shop_id: int, raw: str | None) -> Service | None:
    """The newest service of `shop_id` the pasted link belongs to."""
    token = link_token(raw)
    if token is None:
        return None
    return await session.scalar(
        select(Service)
        .where(Service.shop_id == shop_id, condition(token))
        .order_by(Service.id.desc())
        .limit(1)
    )
