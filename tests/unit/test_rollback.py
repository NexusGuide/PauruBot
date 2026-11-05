"""SEC-F22: a rollback never starts an older release on a schema a newer one migrated.

`pauru rollback` used to switch the image back only. Migrations only go forward, so after an
update that migrated, the old image's migrate service failed on the unknown revision and the
whole stack stayed down. Now the previous image says whether it can run on the database; if it
cannot, the backup the update took before migrating is restored (or nothing changes at all).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from paurubot.db.migrate import schema_state
from tests.unit.test_audit_hardening import installer_function

BACKUP = "/var/lib/paurubot/backups/paurubot-20261101-090000-ab12.pbak"


async def test_schema_state_tells_same_older_and_newer(session) -> None:  # type: ignore[no-untyped-def]
    conn = await session.connection()
    state, current, heads = await schema_state(conn)
    assert state == "same" and current in heads
    await conn.execute(text("UPDATE alembic_version SET version_num = '0031_manual_national'"))
    assert (await schema_state(conn))[0] == "older"  # a later release's migrate moves it forward
    await conn.execute(text("UPDATE alembic_version SET version_num = '9999_from_the_future'"))
    assert (await schema_state(conn))[0] == "newer"  # this release must not run on it
    await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": current})


STUB = r"""
set -Eeuo pipefail
need_root() { :; }
say() { echo "SAY $*"; }
ok() { echo "OK $*"; }
warn() { echo "WARN $*"; }
die() { echo "DIE $*"; exit 17; }
wait_healthy() { return 0; }
confirm() { [[ $CONFIRM == yes ]]; }
compose() {
  echo "COMPOSE image=${PAURUBOT_IMAGE:-env} $*" >>"$LOG"
  case "$*" in
    *schema-status*) return "$SCHEMA" ;;
    *" restore "*) return "$RESTORE" ;;
  esac
}
"""


def _run_rollback(
    tmp_path: Path,
    *,
    schema: int,
    confirm: str = "yes",
    restore: int = 0,
    backup: str = BACKUP,
    agent: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
    env_file, log = tmp_path / ".env", tmp_path / "compose.log"
    env_file.write_text(
        "PAURUBOT_IMAGE=new@sha256:2\nPAURUBOT_VERSION=v2\nPAURUBOT_PREVIOUS_IMAGE=old@sha256:1\n"
        f"PAURUBOT_PREVIOUS_VERSION=v1\nPAURUBOT_PREVIOUS_BACKUP={backup}\n"
    )
    script = (
        STUB
        + f'\nENV_FILE="{env_file}"\nDIR="{tmp_path}"\nLOG="{log}"\nSCHEMA={schema}\nRESTORE={restore}\n'
        + f"CONFIRM={confirm}\n"
        + ("PAURUBOT_AGENT=1\n" if agent else "")
        + "\n".join(
            installer_function(name) for name in ("env_get", "env_set", "swap_release", "cmd_rollback")
        )
        + "\ncmd_rollback\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    return result, values, log.read_text() if log.exists() else ""


def test_compatible_schema_switches_the_image_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result, env, log = _run_rollback(tmp_path, schema=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert env["PAURUBOT_IMAGE"] == "old@sha256:1" and env["PAURUBOT_PREVIOUS_IMAGE"] == "new@sha256:2"
    assert "image=old@sha256:1 run --rm --no-deps -T migrate schema-status" in log  # asked the old image
    assert " restore " not in log


def test_migrated_schema_restores_the_pre_update_backup_with_the_old_release(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result, env, log = _run_rollback(tmp_path, schema=4)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.splitlines()
    stop = next(i for i, line in enumerate(lines) if "stop api bot worker" in line)
    restore = next(i for i, line in enumerate(lines) if f"migrate restore {BACKUP} --yes" in line)
    assert stop < restore  # nothing writes while the database is replaced
    assert env["PAURUBOT_IMAGE"] == "old@sha256:1"  # the old release runs the restore
    assert env["PAURUBOT_PREVIOUS_BACKUP"] == ""  # that backup belonged to the update just undone


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("operator says no", {"schema": 4, "confirm": "no"}),
        ("no backup recorded", {"schema": 4, "backup": ""}),
        ("admin panel agent", {"schema": 4, "agent": True}),
        ("database unreachable", {"schema": 6}),
    ],
)
def test_unsafe_rollbacks_change_nothing(tmp_path, case, kwargs) -> None:  # type: ignore[no-untyped-def]
    result, env, log = _run_rollback(tmp_path, **kwargs)
    assert result.returncode == 17, case
    assert env["PAURUBOT_IMAGE"] == "new@sha256:2", case
    assert " restore " not in log and " stop " not in log and " up " not in log, case


def test_failed_restore_returns_to_the_current_release(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result, env, log = _run_rollback(tmp_path, schema=4, restore=1)
    assert result.returncode == 17
    assert env["PAURUBOT_IMAGE"] == "new@sha256:2" and env["PAURUBOT_PREVIOUS_IMAGE"] == "old@sha256:1"
    assert log.rstrip().endswith("up -d --remove-orphans")  # the current release is started again


def test_update_records_the_backup_a_rollback_would_restore(tmp_path) -> None:  # type: ignore[no-untyped-def]
    env_file = tmp_path / ".env"
    env_file.write_text("PAURUBOT_IMAGE=old@sha256:1\nPAURUBOT_VERSION=v1\nPOSTGRES_APP_PASSWORD=x\n")
    stub = (
        STUB.replace(
            "compose() {",
            "compose() {\n  if [[ $* == *backup* ]]; then printf 'pruned 0\\n"
            + BACKUP
            + " (42 bytes)\\n'; return 0; fi",
        )
        + r"""
resolve_target() { TARGET_REF=v2; TARGET_IMAGE=new; }
fetch_source() { :; }
fetch_image() { PINNED_IMAGE=new@sha256:2; }
link_cli() { echo linked >> "$LOG"; }
"""
    )
    script = (
        stub
        + f'\nENV_FILE="{env_file}"\nDIR="{tmp_path}"\nLOG="{tmp_path / "c.log"}"\nSCHEMA=0\nRESTORE=0\nCONFIRM=yes\n'
        + 'BOLD=""\nRESET=""\n'
        + "\n".join(installer_function(name) for name in ("env_get", "env_set", "cmd_update"))
        + "\ncmd_update\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PAURUBOT_PREVIOUS_BACKUP={BACKUP}" in env_file.read_text()
    # The next `pauru` is the new release's installer (a moved repository, new checks).
    assert "linked" in (tmp_path / "c.log").read_text()


def test_the_cli_is_replaced_by_a_rename_never_rewritten_in_place() -> None:
    """bash reads a running script as it goes: overwriting /usr/local/bin/pauru during
    `pauru update` would corrupt that very run."""
    body = installer_function("link_cli")
    assert "/usr/local/bin/pauru.new" in body and "mv -f /usr/local/bin/pauru.new /usr/local/bin/pauru" in body
