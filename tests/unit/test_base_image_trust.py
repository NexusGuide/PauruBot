"""The database, Redis and proxy images are pinned and the GHCR copies are signed (audit SEC-04, OPS-03).

Our own image was verified with cosign and pinned by digest, but postgres, redis and caddy ran from
moving tags, and the GHCR copies used when Docker Hub is blocked were not signed at all. A swapped
copy would run the database with every customer's data. The release now signs each copy, the
installer refuses an unsigned one, and both sources are pinned by digest in .env. The release
workflow also grants write permissions only to the jobs that push and sign.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _release() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


def _function(name: str) -> str:
    text = (ROOT / "install.sh").read_text()
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", text, re.S | re.M)
    assert match, name
    return match.group(1)


def test_the_release_signs_every_base_image_copy() -> None:
    mirror = _release()["jobs"]["mirror"]
    steps = " ".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in mirror["steps"])
    assert "sigstore/cosign-installer@" in steps
    assert 'cosign sign --yes "${target}@${digest}"' in steps
    assert mirror["permissions"]["id-token"] == "write"


def test_the_installer_verifies_a_copy_and_pins_both_sources_by_digest() -> None:
    body = _function("pick_base_images")
    assert "verify_signature" in body, "a GHCR copy runs only with a valid release signature"
    assert body.count("pinned_digest") >= 2, "Docker Hub and GHCR images are both pinned by digest"
    verify = _function("verify_signature")
    assert "--certificate-identity-regexp" in verify and "token.actions.githubusercontent.com" in verify
    assert "verify_signature" in _function("fetch_image"), "our image keeps the same check"


def test_write_permissions_belong_to_the_jobs_that_publish() -> None:
    release = _release()
    assert release["permissions"] == {"contents": "read"}
    image = release["jobs"]["image"]["permissions"]
    assert image == {"contents": "write", "packages": "write", "id-token": "write"}
    assert release["jobs"]["mirror"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }


@pytest.mark.parametrize("mode", ["hub", "copy_signed", "copy_unsigned", "copy_no_digest", "none"])
def test_base_images_run_pinned_and_a_copy_only_when_signed(mode: str) -> None:
    import subprocess

    from tests.unit.test_audit_hardening import installer_function

    stub = r"""
set -Eeuo pipefail
REPO=NexusGuide/PauruBot
ok() { :; }
die() { exit 17; }
install_cosign() { :; }
env_set() { printf "%s=%s\n" "$1" "$2"; }
docker() {
  if [[ "$1" == pull ]]; then
    case "$MODE:$3" in
      hub:*) [[ "$3" != ghcr.io/* ]] ;;
      copy_*:ghcr.io/*) return 0 ;;
      *) return 1 ;;
    esac
    return
  fi
  [[ "$MODE" == copy_no_digest ]] || echo "${5%%:*}@sha256:pinned"
}
cosign() { [[ "$MODE" != copy_unsigned ]]; }
"""
    script = (
        stub
        + f"\nMODE={mode}\n"
        + installer_function("verify_signature")
        + "\n"
        + installer_function("pinned_digest")
        + "\n"
        + installer_function("pick_base_images")
        + "\npick_base_images\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    if mode in {"copy_unsigned", "copy_no_digest", "none"}:
        assert result.returncode == 17, result.stderr
        return
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3 and all("@sha256:pinned" in line for line in lines), lines
    if mode == "copy_signed":
        assert all("paurubot-mirror@sha256:pinned" in line for line in lines), lines
