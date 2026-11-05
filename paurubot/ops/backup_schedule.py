"""When automatic backups run, as the owner sets it on the Backups page (OPS-05).

The schedule is server-wide, so it lives in the root shop's settings; a value never set there
falls back to the BACKUP_* environment setting, which keeps older installs behaving as before.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.config import Settings, get_settings

KEYS = (
    "backup_auto_enabled",
    "backup_every_hours",
    "backup_hour",
    "backup_keep",
    "backup_send_telegram",
)
MIN_HOURS, MAX_HOURS = 1, 168
# The worker looks every ten minutes; a run may come one look early rather than one look late.
TICK = timedelta(minutes=10)
# Daily and longer: at the set hour, unless a backup was already made in the last stretch.
DAILY_SLACK = timedelta(hours=4)
# After a failed automatic backup, wait this long before the next try, so a lasting failure
# (a full disk) is not retried and reported every ten minutes.
RETRY_KEY = "backups:auto:failed"
RETRY_AFTER = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Schedule:
    enabled: bool
    every_hours: int
    hour: int
    keep: int
    telegram: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective(cfg: dict[str, Any], env: Settings | None = None) -> Schedule:
    env = env or get_settings()

    def pick(key: str, fallback: Any) -> Any:
        value = cfg.get(key)
        return fallback if value is None else value

    every = int(pick("backup_every_hours", 24))
    return Schedule(
        enabled=bool(pick("backup_auto_enabled", True)),
        every_hours=min(max(every, MIN_HOURS), MAX_HOURS),
        hour=int(pick("backup_hour", env.backup_hour)) % 24,
        keep=max(int(pick("backup_keep", env.backup_keep)), 1),
        telegram=bool(pick("backup_send_telegram", env.backup_to_telegram)),
    )


async def load(session: AsyncSession) -> Schedule:
    from paurubot.core import settings as shop_settings
    from paurubot.core.shops import root_shop

    shop = await root_shop(session)
    cfg = await shop_settings.load(session, shop.id, fresh=True) if shop is not None else {}
    return effective(cfg)


async def current() -> Schedule:
    from paurubot.db.session import session_scope

    async with session_scope() as session:
        return await load(session)


def due(schedule: Schedule, *, now: datetime, local_hour: int, newest: datetime | None) -> bool:
    """Whether an automatic backup should be made now, given when the newest one was made."""
    if not schedule.enabled:
        return False
    every = timedelta(hours=schedule.every_hours)
    if schedule.every_hours >= 24:
        if local_hour != schedule.hour:
            return False
        gap = every - DAILY_SLACK
    else:
        gap = every - TICK
    return newest is None or now - newest >= gap
