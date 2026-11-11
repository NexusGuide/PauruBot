"""Broadcast controls (F-16): pause, resume, schedule and why messages did not arrive.

Each resume starts a new run. The job carries the run number and stops as soon as the broadcast
has moved on to another run, so a paused job that is still finishing its batch and the job that
resumes it never send to the same people.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import affected, utcnow
from paurubot.db.models import Broadcast, JobStatus
from paurubot.worker import queue

# Buttons that open a section of the bot instead of a link (Foxima parity): the main-menu item
# each one opens. "home" brings back the main menu.
ACTIONS = ("home", "buy", "services", "wallet", "trial", "referral", "tickets", "guide")
IDLE_DAYS_MAX = 365


def valid_button(button: Any) -> bool:
    """A link button (`text` + https/tg `url`) or a section button (`text` + `action`)."""
    if not isinstance(button, dict) or not str(button.get("text") or "").strip():
        return False
    if "action" in button:
        return button["action"] in ACTIONS and "url" not in button
    return str(button.get("url") or "").startswith(("https://", "tg://"))


def markup(buttons: list[dict[str, Any]] | None) -> Any:
    """The keyboard under a broadcast: link buttons as before, section buttons as menu presses."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from paurubot.bot.callbacks import Menu, Nav

    rows = []
    for button in buttons or []:
        if not valid_button(button):
            continue
        text = str(button["text"])[:64]
        action = button.get("action")
        if action == "home":
            rows.append([InlineKeyboardButton(text=text, callback_data=Nav(to="home").pack())])
        elif action:
            rows.append([InlineKeyboardButton(text=text, callback_data=Menu(item=str(action)).pack())])
        else:
            rows.append([InlineKeyboardButton(text=text, url=str(button["url"]))])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


PAUSABLE = (JobStatus.QUEUED, JobStatus.RUNNING)
FINISHED = (JobStatus.DONE, JobStatus.CANCELED)
# Longer than one runner batch, so a job told to stop has finished before the next one starts.
RESUME_DELAY = timedelta(seconds=15)
MAX_SCHEDULE_AHEAD = timedelta(days=60)


def schedule_ok(when: datetime | None, now: datetime | None = None) -> bool:
    if when is None:
        return True
    now = now or utcnow()
    if when.tzinfo is None:
        return False
    return now < when <= now + MAX_SCHEDULE_AHEAD


async def start(session: AsyncSession, row: Broadcast) -> None:
    """Queue the first run, at the scheduled time when there is one."""
    row.status = JobStatus.QUEUED
    await queue.enqueue(
        session,
        "broadcast",
        "run",
        {"broadcast_id": row.id, "run": row.run or 0},
        shop_id=row.shop_id,
        dedupe_key=f"broadcast:{row.id}",
        run_at=row.scheduled_at,
    )


async def pause(session: AsyncSession, row: Broadcast) -> bool:
    result = await session.execute(
        update(Broadcast)
        .where(Broadcast.id == row.id, Broadcast.status.in_(PAUSABLE))
        .values(status=JobStatus.PAUSED)
    )
    await session.refresh(row)
    return bool(affected(result))


async def resume(session: AsyncSession, row: Broadcast) -> bool:
    result = await session.execute(
        update(Broadcast)
        .where(Broadcast.id == row.id, Broadcast.status == JobStatus.PAUSED)
        .values(status=JobStatus.QUEUED, run=Broadcast.run + 1)
    )
    if not affected(result):
        await session.refresh(row)
        return False
    await session.refresh(row)
    run_at = utcnow() + RESUME_DELAY
    if row.scheduled_at is not None and row.scheduled_at > run_at:
        run_at = row.scheduled_at  # paused before its time: keep the schedule
    await queue.enqueue(
        session,
        "broadcast",
        "run",
        {"broadcast_id": row.id, "run": row.run},
        shop_id=row.shop_id,
        dedupe_key=f"broadcast:{row.id}:run:{row.run}",
        run_at=run_at,
    )
    return True


async def cancel(session: AsyncSession, row: Broadcast) -> bool:
    result = await session.execute(
        update(Broadcast)
        .where(Broadcast.id == row.id, Broadcast.status.not_in(FINISHED))
        .values(status=JobStatus.CANCELED)
    )
    await session.refresh(row)
    return bool(affected(result))


def should_stop(row: Broadcast, run: int) -> bool:
    """Checked by the runner before each batch."""
    return row.status in (*FINISHED, JobStatus.PAUSED) or int(row.run or 0) != int(run)


def undeliverable_reason(error: Exception | str) -> str:
    """`blocked` when the customer blocked the bot; `deleted` for a gone account or chat."""
    return "blocked" if "blocked" in str(error).lower() else "deleted"


def progress(row: Broadcast) -> dict[str, Any]:
    return {
        "status": str(row.status),
        "total": row.total or 0,
        "sent": row.sent or 0,
        "failed": row.failed or 0,
        "blocked": row.blocked or 0,
        "deleted": row.deleted or 0,
        "skipped": getattr(row, "skipped", 0) or 0,
        "left": 0
        if row.status in FINISHED
        else max(
            0, (row.total or 0) - (row.sent or 0) - (row.failed or 0) - (getattr(row, "skipped", 0) or 0)
        ),
    }
