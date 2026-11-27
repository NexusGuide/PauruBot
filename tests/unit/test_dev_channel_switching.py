"""End-to-end verification of switching between main (stable/beta) and dev channels."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path


from paurubot import __version__
from paurubot.ops import updater

ROOT = Path(__file__).resolve().parents[2]
BACKUP_1 = "/var/lib/paurubot/backups/paurubot-20261101-090000-predev.pbak"
BACKUP_2 = "/var/lib/paurubot/backups/paurubot-20261102-090000-dev2.pbak"
BACKUP_3 = "/var/lib/paurubot/backups/paurubot-20261103-090000-dev3.pbak"


def _fn(name: str) -> str:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + 2
    return text[start:end]


BASE_STUB = r"""
set -Eeuo pipefail
BOLD=""
RESET=""
IMAGE_REPO="ghcr.io/nexusguide/paurubot"
need_root() { :; }
say() { echo "SAY $*"; }
ok() { echo "OK $*"; }
warn() { echo "WARN $*"; }
die() { echo "DIE $*"; exit 17; }
wait_healthy() { return 0; }
confirm() { [[ ${CONFIRM:-yes} == yes ]]; }
random_secret() { echo "secret1234567890"; }
latest_release() { echo "v0.5.7"; }
fetch_source() {
  echo "FETCH_SOURCE $1" >>"$LOG"
  if [[ $1 == dev ]]; then
    printf 'version = "0.1.1-dev"\n' >"$DIR/pyproject.toml"
  else
    printf 'version = "0.5.7"\n' >"$DIR/pyproject.toml"
  fi
}
fetch_image() {
  echo "FETCH_IMAGE $1" >>"$LOG"
  PINNED_IMAGE="${1}@sha256:pinned"
}
link_cli() { echo "LINK_CLI" >>"$LOG"; }
compose() {
  echo "COMPOSE image=${PAURUBOT_IMAGE:-env} $*" >>"$LOG"
  case "$*" in
    *" paurubot backup"*)
      printf 'pruned 0\n%s (42 bytes)\n' "$NEXT_BACKUP"
      return 0
      ;;
    *schema-status*) return "${SCHEMA:-0}" ;;
    *" restore "*) return "${RESTORE:-0}" ;;
  esac
}
"""


def _read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def _run_bash(tmp_path: Path, body: str, env_vars: str = "") -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env"
    log_file = tmp_path / "ops.log"
    (tmp_path / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    fns = "\n".join(
        _fn(name) for name in ("env_get", "env_set", "resolve_target", "swap_release", "cmd_update", "cmd_rollback", "rcli")
    )
    script = (
        BASE_STUB
        + f'\nENV_FILE="{env_file}"\nDIR="{tmp_path}"\nLOG="{log_file}"\n'
        + env_vars
        + "\n"
        + fns
        + "\n"
        + body
        + "\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)


def test_dev_release_version_is_0_1_1_dev_everywhere() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
    assert __version__ == "0.1.1-dev"
    assert pyproject["project"]["version"] == "0.1.1-dev"
    assert pkg["version"] == "0.1.1-dev"
    assert lock["version"] == "0.1.1-dev"
    assert lock["packages"][""]["version"] == "0.1.1-dev"
    assert updater.VERSION_RE.match(__version__)


def test_updater_channel_aware_update_available_for_dev() -> None:
    # On dev 0.1.1-dev, a stale stable tag like v0.5.7 must NOT trigger update_available.
    assert not updater._update_available("v0.5.7", "0.1.1-dev", "dev", "0.1.1-dev")
    assert not updater._update_available("0.1.1-dev", "0.1.1-dev", "dev", "0.1.1-dev")
    assert updater._update_available("0.1.2-dev", "0.1.1-dev", "dev", "0.1.1-dev")
    assert updater._update_available("0.1.1-dev.2", "0.1.1-dev", "dev", "0.1.1-dev")
    # Even if PAURUBOT_VERSION was literally "dev", app_version 0.1.1-dev is used.
    assert updater._update_available("0.1.2-dev", "dev", "dev", "0.1.1-dev")
    assert not updater._update_available("0.1.1-dev", "dev", "dev", "0.1.1-dev")
    # On stable channel, normal semver comparison still holds.
    assert updater._update_available("v0.5.8", "v0.5.7", "stable", "0.5.7")


def test_main_to_dev_then_repeated_dev_then_return_to_main_with_restore(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAURUBOT_IMAGE=ghcr.io/nexusguide/paurubot:0.5.7@sha256:main\n"
        "PAURUBOT_VERSION=v0.5.7\n"
        "PAURUBOT_CHANNEL=stable\n"
        "PAURUBOT_REF=v0.5.7\n"
        "POSTGRES_APP_PASSWORD=p\n"
        "REDIS_PASSWORD=r\n",
        encoding="utf-8",
    )

    # 1) Switch stable (main) -> dev
    res = _run_bash(tmp_path, "cmd_update --channel dev", f'NEXT_BACKUP="{BACKUP_1}"\nSCHEMA=3\n')
    assert res.returncode == 0, res.stdout + res.stderr
    env = _read_env(env_file)
    assert env["PAURUBOT_CHANNEL"] == "dev"
    assert env["PAURUBOT_VERSION"] == "0.1.1-dev"
    assert env["PAURUBOT_REF"] == "dev"
    assert env["PAURUBOT_PREVIOUS_CHANNEL"] == "stable"
    assert env["PAURUBOT_PRE_DEV_BACKUP"] == BACKUP_1
    assert env["PAURUBOT_PRE_DEV_CHANNEL"] == "stable"

    # 2) Update again on dev (must keep PAURUBOT_PRE_DEV_BACKUP intact!)
    res2 = _run_bash(tmp_path, "cmd_update", f'NEXT_BACKUP="{BACKUP_2}"\nSCHEMA=0\n')
    assert res2.returncode == 0, res2.stdout + res2.stderr
    env2 = _read_env(env_file)
    assert env2["PAURUBOT_CHANNEL"] == "dev"
    assert env2["PAURUBOT_PREVIOUS_BACKUP"] == BACKUP_2
    assert env2["PAURUBOT_PRE_DEV_BACKUP"] == BACKUP_1  # preserved across repeated dev updates

    # 3) Switching dev -> stable from admin panel agent when schema migrated (exit 4) must refuse cleanly
    res_agent = _run_bash(
        tmp_path,
        "cmd_update --channel stable",
        f'NEXT_BACKUP="{BACKUP_3}"\nSCHEMA=4\nPAURUBOT_AGENT=1\n',
    )
    assert res_agent.returncode == 17
    env_after_refusal = _read_env(env_file)
    assert env_after_refusal["PAURUBOT_CHANNEL"] == "dev"
    assert env_after_refusal["PAURUBOT_PRE_DEV_BACKUP"] == BACKUP_1

    # 4) Switching dev -> stable from CLI restores PAURUBOT_PRE_DEV_BACKUP and switches back cleanly
    log_file = tmp_path / "ops.log"
    log_file.write_text("", encoding="utf-8")
    res_back = _run_bash(
        tmp_path,
        "cmd_update --channel stable",
        f'NEXT_BACKUP="{BACKUP_3}"\nSCHEMA=4\nCONFIRM=yes\nRESTORE=0\n',
    )
    assert res_back.returncode == 0, res_back.stdout + res_back.stderr
    env_back = _read_env(env_file)
    log_text = log_file.read_text(encoding="utf-8")
    assert env_back["PAURUBOT_CHANNEL"] == "stable"
    assert env_back["PAURUBOT_VERSION"] == "v0.5.7"
    assert env_back["PAURUBOT_REF"] == "v0.5.7"
    assert env_back["PAURUBOT_PRE_DEV_BACKUP"] == ""
    assert f"migrate restore {BACKUP_1} --yes" in log_text
    assert "FETCH_SOURCE v0.5.7" in log_text
    assert "LINK_CLI" in log_text


def test_rollback_from_dev_restores_channel_ref_and_clears_pre_dev_backup(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAURUBOT_IMAGE=ghcr.io/nexusguide/paurubot:dev@sha256:dev\n"
        "PAURUBOT_VERSION=0.1.1-dev\n"
        "PAURUBOT_CHANNEL=dev\n"
        "PAURUBOT_REF=dev\n"
        "PAURUBOT_PREVIOUS_IMAGE=ghcr.io/nexusguide/paurubot:0.5.7@sha256:main\n"
        "PAURUBOT_PREVIOUS_VERSION=v0.5.7\n"
        "PAURUBOT_PREVIOUS_CHANNEL=stable\n"
        "PAURUBOT_PREVIOUS_REF=v0.5.7\n"
        f"PAURUBOT_PREVIOUS_BACKUP={BACKUP_1}\n"
        f"PAURUBOT_PRE_DEV_BACKUP={BACKUP_1}\n",
        encoding="utf-8",
    )
    res = _run_bash(tmp_path, "cmd_rollback", "SCHEMA=4\nCONFIRM=yes\nRESTORE=0\n")
    assert res.returncode == 0, res.stdout + res.stderr
    env = _read_env(env_file)
    log_text = (tmp_path / "ops.log").read_text(encoding="utf-8")
    assert env["PAURUBOT_CHANNEL"] == "stable"
    assert env["PAURUBOT_PREVIOUS_CHANNEL"] == "dev"
    assert env["PAURUBOT_VERSION"] == "v0.5.7"
    assert env["PAURUBOT_REF"] == "v0.5.7"
    assert env["PAURUBOT_PREVIOUS_BACKUP"] == ""
    assert env["PAURUBOT_PRE_DEV_BACKUP"] == ""
    assert "FETCH_SOURCE v0.5.7" in log_text
    assert "LINK_CLI" in log_text


def test_failed_restore_during_dev_to_main_reverts_everything(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAURUBOT_IMAGE=ghcr.io/nexusguide/paurubot:dev@sha256:dev\n"
        "PAURUBOT_VERSION=0.1.1-dev\n"
        "PAURUBOT_CHANNEL=dev\n"
        "PAURUBOT_REF=dev\n"
        "PAURUBOT_PREVIOUS_IMAGE=ghcr.io/nexusguide/paurubot:0.5.7@sha256:main\n"
        "PAURUBOT_PREVIOUS_VERSION=v0.5.7\n"
        "PAURUBOT_PREVIOUS_CHANNEL=stable\n"
        "PAURUBOT_PREVIOUS_REF=v0.5.7\n"
        f"PAURUBOT_PREVIOUS_BACKUP={BACKUP_1}\n"
        f"PAURUBOT_PRE_DEV_BACKUP={BACKUP_1}\n"
        "POSTGRES_APP_PASSWORD=p\n"
        "REDIS_PASSWORD=r\n",
        encoding="utf-8",
    )
    res = _run_bash(
        tmp_path,
        "cmd_update --channel stable",
        f'NEXT_BACKUP="{BACKUP_2}"\nSCHEMA=4\nCONFIRM=yes\nRESTORE=1\n',
    )
    assert res.returncode == 17
    env = _read_env(env_file)
    assert env["PAURUBOT_CHANNEL"] == "dev"
    assert env["PAURUBOT_VERSION"] == "0.1.1-dev"
    assert env["PAURUBOT_PREVIOUS_BACKUP"] == BACKUP_1
    assert env["PAURUBOT_PRE_DEV_BACKUP"] == BACKUP_1


def test_agent_rcli_passes_redis_password(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("REDIS_PASSWORD=supersecret\n", encoding="utf-8")
    res = _run_bash(tmp_path, "rcli PING")
    assert res.returncode == 0, res.stdout + res.stderr
    log_text = (tmp_path / "ops.log").read_text(encoding="utf-8")
    assert "REDISCLI_AUTH=supersecret" in log_text
    assert "--no-auth-warning PING" in log_text


def test_release_workflow_tags_dev_version_and_marks_prereleases() -> None:
    rel = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "DEV_VERSION" in rel
    assert "IS_PRERELEASE" in rel
    assert "--prerelease" in rel
    assert re.search(r"type=raw,value=dev,enable=\$\{\{ github\.ref == 'refs/heads/dev'", rel)
