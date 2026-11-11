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


# Persian and Arabic letters to Latin, so a Persian first name can still name a config.
_LATIN = str.maketrans(
    {
        "\u0627": "a",
        "\u0622": "a",
        "\u0623": "a",
        "\u0625": "e",
        "\u0628": "b",
        "\u067e": "p",
        "\u062a": "t",
        "\u062b": "s",
        "\u062c": "j",
        "\u0686": "ch",
        "\u062d": "h",
        "\u062e": "kh",
        "\u062f": "d",
        "\u0630": "z",
        "\u0631": "r",
        "\u0632": "z",
        "\u0698": "zh",
        "\u0633": "s",
        "\u0634": "sh",
        "\u0635": "s",
        "\u0636": "z",
        "\u0637": "t",
        "\u0638": "z",
        "\u0639": "a",
        "\u063a": "gh",
        "\u0641": "f",
        "\u0642": "gh",
        "\u06a9": "k",
        "\u0643": "k",
        "\u06af": "g",
        "\u0644": "l",
        "\u0645": "m",
        "\u0646": "n",
        "\u0648": "v",
        "\u0647": "h",
        "\u0629": "h",
        "\u06cc": "i",
        "\u064a": "i",
        "\u0626": "i",
        "\u0624": "o",
        "\u0621": "",
        "\u200c": "",
        "\u06f0": "0",
        "\u06f1": "1",
        "\u06f2": "2",
        "\u06f3": "3",
        "\u06f4": "4",
        "\u06f5": "5",
        "\u06f6": "6",
        "\u06f7": "7",
        "\u06f8": "8",
        "\u06f9": "9",
    }
)


def latin(text: str | None) -> str:
    """A name in Latin letters: Persian/Arabic letters spelled out, everything else kept."""
    return (text or "").translate(_LATIN)


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
    elif method == UsernameMethod.CUSTOMER_NAME:
        # Never a bare name: the random tail keeps two «ali»s (or a renamed one) apart.
        body = (
            slug(customer.username, 12) or slug(latin(customer.first_name), 12) or str(customer.telegram_id)
        )
        tail = secrets.token_hex(3)
    elif method == UsernameMethod.CUSTOM_LABEL:
        body = slug(label) or str(customer.telegram_id)
    else:
        body = str(customer.telegram_id)
    parts = [p for p in (prefix + body, tail) if p]
    name = "_".join(parts)
    if len(name) < 3:
        name = f"u_{name}"
    return await _with_suffix(session, panel.shop_id, name)


async def _with_suffix(session: AsyncSession, shop_id: int, name: str) -> str:
    """The shop's fixed suffix after the name, the name shortened to keep it within the limit."""
    from paurubot.core import settings as shop_settings

    suffix = slug(str((await shop_settings.load(session, shop_id)).get("username_suffix") or ""), 8)
    if not suffix:
        return name[:MAX_LEN]
    return f"{name[: MAX_LEN - len(suffix) - 1]}_{suffix}"


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
