"""Uploaded dumps from another bot are deleted once used, and old ones expire (audit SEC-03).

A MySQL dump or SQLite file holds the old bot's customers, phone numbers and balances in plain
form. It stayed in the backups volume forever. Now the real import deletes it, a dry run keeps it
for the run that follows, and anything left over is removed after a few days.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from paurubot.config import get_settings
from paurubot.migration import runner
from paurubot.worker import jobs, queue
from tests import factories
from tests.unit.test_migration import MIRZA_DUMP


def _upload(token: str) -> Path:
    path = runner.upload_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MIRZA_DUMP)
    return path


def _job(shop_id: int, token: str, *, dry_run: bool) -> queue.Claimed:
    payload = {"token": token, "source": None, "panel_map": {}, "dry_run": dry_run, "shop_id": shop_id}
    return queue.Claimed(1, "migrate", payload, 1, 2, shop_id, "lease")


async def test_a_real_import_deletes_the_upload_and_a_dry_run_keeps_it(
    session, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path))
    shop = await factories.shop(session)
    await session.commit()
    path = _upload("a" * 24)

    await jobs.migrate_bot(session, _job(shop.id, "a" * 24, dry_run=True))
    assert path.is_file(), "a dry run is followed by the real run, which needs the file"

    await jobs.migrate_bot(session, _job(shop.id, "a" * 24, dry_run=False))
    assert not path.exists(), "the customers' data does not stay on disk after the import"


def test_old_uploads_expire(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path))
    old, fresh = _upload("b" * 24), _upload("c" * 24)
    stale = time.time() - runner.UPLOAD_TTL - 60
    os.utime(old, (stale, stale))
    stray = runner.imports_dir() / "notes.txt"
    stray.write_text("not ours")

    assert runner.prune_uploads() == 1
    assert not old.exists() and fresh.is_file() and stray.is_file()


def test_the_worker_prunes_uploads_on_a_schedule() -> None:
    from paurubot.worker.runner import PERIODIC

    assert "migration_uploads.cleanup" in {job.name for job in PERIODIC}
