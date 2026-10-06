"""Panel username generation methods (CAT-09)."""

from __future__ import annotations

import re
import secrets

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Customer, Panel, UsernameMethod

_CLEAN = re.compile(r"[^a-z0-9_]+")
MAX_LEN = 32


def slug(text: str | None, limit: int = 16) -> str:
    value = (text or "").lower().replace(" ", "_").replace("-", "_")
    return _CLEAN.sub("", value)[:limit].strip("_")


async def _next_sequence(session: AsyncSession, panel: Panel) -> int:
    value = await session.scalar(
        update(Panel)
        .where(Panel.id == panel.id)
        .values(username_sequence=Panel.username_sequence + 1)
        .returning(Panel.username_sequence)
    )
    return int(value or 1)


async def generate(session: AsyncSession, panel: Panel, customer: Customer, label: str | None = None) -> str:
    """Build a panel-safe username ([a-z0-9_], 3..32 chars) with a random tail against collisions."""
    method = UsernameMethod(panel.username_method)
    prefix = slug(panel.username_prefix, 8)
    tail = secrets.token_hex(2)
    if method == UsernameMethod.TELEGRAM_USERNAME:
        body = slug(customer.username) or str(customer.telegram_id)
    elif method == UsernameMethod.RANDOM:
        body = secrets.token_hex(4)
    elif method == UsernameMethod.SEQUENTIAL:
        body = str(await _next_sequence(session, panel))
        tail = ""
    elif method == UsernameMethod.CUSTOM_LABEL:
        body = slug(label) or str(customer.telegram_id)
    else:
        body = str(customer.telegram_id)
    parts = [p for p in (prefix + body, tail) if p]
    name = "_".join(parts)
    if len(name) < 3:
        name = f"u_{name}"
    return name[:MAX_LEN]
