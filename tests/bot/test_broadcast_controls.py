"""F-16 through the real dispatcher: test send, pause, resume and the blocked count."""

from __future__ import annotations

from aiogram.methods import EditMessageText
from sqlalchemy import select, update

from paurubot.bot.callbacks import Admin
from paurubot.db.base import utcnow
from paurubot.db.models import Broadcast, Job, JobStatus
from tests.bot.test_admin_tools import FA, OWNER, env  # noqa: F401  (shared fixture)
from tests.fakes.telegram import callback_update, message_update
from tests.unit.test_resellers import drain


async def test_pause_resume_and_blocked(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    reader, gone = 7701, 7702
    await send(message_update(reader, "/start"))
    await send(message_update(gone, "/start"))
    fake.blocked.add(gone)
    await send(callback_update(OWNER, Admin(section="broadcast").pack()))
    await send(message_update(OWNER, "Weekend sale"))
    row = await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()))
    assert row is not None

    fake.reset()
    await send(callback_update(OWNER, Admin(section="bc", action="test", id=row.id).pack()))
    copies = [c for c in fake.calls if type(c).__name__ == "CopyMessage"]
    assert [c.chat_id for c in copies] == [OWNER]  # only the staff member sees the test
    await session.refresh(row)
    assert row.status == JobStatus.DRAFT

    await send(callback_update(OWNER, Admin(section="bc", action="send", id=row.id).pack()))
    assert FA("adm_bc_pause") in [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    await send(callback_update(OWNER, Admin(section="bcc", action="pause", id=row.id).pack()))
    await session.refresh(row)
    assert row.status == JobStatus.PAUSED
    fake.reset()
    await drain(session, "broadcast")
    assert not [c for c in fake.calls if type(c).__name__ == "CopyMessage"]  # paused: nobody hears it

    await send(callback_update(OWNER, Admin(section="bcc", action="resume", id=row.id).pack()))
    await session.refresh(row)
    assert row.status == JobStatus.QUEUED and row.run == 1
    # The resumed run waits a moment so a stopping job can finish its batch; skip the wait here.
    await session.execute(
        update(Job).where(Job.kind == "run", Job.status == JobStatus.QUEUED).values(run_at=utcnow())
    )
    await session.commit()
    await drain(session, "broadcast")
    await session.refresh(row)
    chats = {c.chat_id for c in fake.calls if type(c).__name__ == "CopyMessage"}
    assert reader in chats and OWNER in chats
    assert row.status == JobStatus.DONE
    assert row.blocked == 1 and row.deleted == 0 and row.failed == 1
