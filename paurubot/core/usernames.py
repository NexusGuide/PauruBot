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
_WANTED = re.compile(r"^[a-z][a-z0-9_]{2,15}$")


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


def wanted(text: str | None) -> str | None:
    """A name the customer chose for its service (CAT-12): 3..16 of [a-z0-9_], starting with a
    letter, or None when it doesn't fit."""
    value = (text or "").strip().lower()
    return value if _WANTED.match(value) and "__" not in value else None


async def chosen(session: AsyncSession, panel: Panel, name: str, prefix: str | None = None) -> str:
    """The customer's own name on this panel, with a random tail only when it is already taken
    there: an account the bot did not create is never handed to someone else."""
    from sqlalchemy import select

    from paurubot.db.models import Service
    from paurubot.panels import PanelError, PanelRef, driver_for

    base = (slug(prefix if prefix else panel.username_prefix, 8) + (wanted(name) or "")).strip("_")
    if len(base) < 3:
        base = f"u_{base}"
    base = base[:MAX_LEN]
    for attempt in range(4):
        candidate = base if attempt == 0 else f"{base[: MAX_LEN - 5]}_{secrets.token_hex(2)}"
        taken = await session.scalar(
            select(Service.id).where(Service.panel_id == panel.id, Service.panel_username == candidate)
        )
        if taken is not None:
            continue
        try:
            if await (await driver_for(panel)).get_user(PanelRef(candidate, {})) is None:
                return candidate
        except PanelError:
            break  # can't tell whether it is free: take a random tail
    return f"{base[: MAX_LEN - 9]}_{secrets.token_hex(4)}"
