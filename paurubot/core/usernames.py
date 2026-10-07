"""Panel username generation methods (CAT-09)."""

from __future__ import annotations

import re
import secrets

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Customer, Panel, UsernameMethod

_CLEAN = re.compile(r"[^a-z0-9_]+")
MAX_LEN = 32


_RUNS = re.compile(r"_{2,}")


def slug(text: str | None, limit: int = 16) -> str:
    """Panels such as PasarGuard refuse two special characters in a row, so runs become one `_`."""
    value = (text or "").lower().replace(" ", "_").replace("-", "_")
    return _RUNS.sub("_", _CLEAN.sub("", value))[:limit].strip("_")


async def _next_sequence(session: AsyncSession, panel: Panel) -> int:
    value = await session.scalar(
        update(Panel)
        .where(Panel.id == panel.id)
        .values(username_sequence=Panel.username_sequence + 1)
        .returning(Panel.username_sequence)
    )
    return int(value or 1)


async def generate(
    session: AsyncSession,
    panel: Panel,
    customer: Customer,
    label: str | None = None,
    prefix: str | None = None,
) -> str:
    """Build a panel-safe username ([a-z0-9_], 3..32 chars) with a random tail against collisions.

    `prefix` (a plan's own prefix, CAT-02) replaces the panel's prefix when given.
    """
    method = UsernameMethod(panel.username_method)
    prefix = slug(prefix if prefix else panel.username_prefix, 8)
    tail = secrets.token_hex(2)
    if customer.telegram_id < 0:  # a web guest has no id worth showing (CAT-08)
        body = "g" + secrets.token_hex(4)
    elif method == UsernameMethod.TELEGRAM_USERNAME:
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
