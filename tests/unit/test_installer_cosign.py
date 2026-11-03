"""The installer brings its own cosign, and a half-finished install can be run again.

The installer refused every image without cosign but never installed it, so on a fresh server the
first install stopped with "cosign is required" after writing .env; running it again then said
"already installed". These tests run the real installer functions with a stand-in curl.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCH = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, "amd64")


def _run(
    tmp: Path, script: str, *, release: dict[str, bytes] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source install.sh without its main call and run `script`; curl serves files from `release`."""
    served = tmp / "release"
    served.mkdir(exist_ok=True)
    for name, data in (release or {}).items():
        (served / name).write_bytes(data)
    shims = tmp / "bin"
    shims.mkdir(exist_ok=True)
    curl = shims / "curl"
    curl.write_text(
        '#!/bin/bash\nout=""; url=""\nwhile (($#)); do case $1 in -o) out=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n'
        f'echo "$url" >> "{tmp}/fetched"\nsrc="{served}/$(basename "$url")"\n[[ -f $src ]] || exit 22\ncp "$src" "$out"\n'
    )
    curl.chmod(0o755)
    body = "\n".join((ROOT / "install.sh").read_text().splitlines()[:-1])  # drop `main "$@"`
    env = {
        "PATH": f"{shims}:/usr/bin:/bin",
        "PAURUBOT_DIR": str(tmp / "opt"),
        "PAURUBOT_COSIGN_URL": "https://example.invalid/download",
        "PAURUBOT_COSIGN_BIN": str(shims / "cosign"),
        "HOME": str(tmp),
    }
    return subprocess.run(
        ["bash", "-c", body + "\n" + script], capture_output=True, text=True, env=env, timeout=30
    )


def _release(binary: bytes, *, listed: bytes | None = None) -> dict[str, bytes]:
    digest = hashlib.sha256(listed if listed is not None else binary).hexdigest()
    sums = f"{'0' * 64}  cosign-linux-other\n{digest}  cosign-linux-{ARCH}\n".encode()
    return {f"cosign-linux-{ARCH}": binary, "cosign_checksums.txt": sums}


def test_a_fresh_server_gets_a_checksum_verified_cosign(tmp_path) -> None:  # type: ignore[no-untyped-def]
    binary = b"#!/bin/sh\necho cosign-ok\n"
    result = _run(tmp_path, "install_cosign && cosign", release=_release(binary))
    assert result.returncode == 0, result.stderr
    assert "cosign-ok" in result.stdout
    assert (tmp_path / "bin" / "cosign").read_bytes() == binary


def test_a_cosign_that_does_not_match_its_checksum_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    release = _release(b"#!/bin/sh\necho tampered\n", listed=b"the real build")
    result = _run(tmp_path, "install_cosign", release=release)
    assert result.returncode != 0 and "does not match its published checksum" in result.stderr
    assert not (tmp_path / "bin" / "cosign").exists()


def test_an_installed_cosign_is_left_alone(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "bin").mkdir()
    existing = tmp_path / "bin" / "cosign"
    existing.write_text("#!/bin/sh\necho mine\n")
    existing.chmod(0o755)
    result = _run(tmp_path, "install_cosign && cosign")
    assert result.returncode == 0 and "mine" in result.stdout
    assert not (tmp_path / "fetched").exists()  # nothing downloaded


STUB_INSTALL = """
need_root() { :; }
check_system() { :; }
install_docker() { :; }
resolve_target() { echo REACHED_DOWNLOAD; exit 0; }
cmd_install --domain bot.example.com --token 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh --owner 42
"""


def test_an_install_that_stopped_before_starting_anything_can_run_again(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / ".env").write_text("DOMAIN=bot.example.com\nBOT_TOKEN=x\n")  # no PAURUBOT_IMAGE yet
    result = _run(tmp_path, STUB_INSTALL)
    assert result.returncode == 0 and "REACHED_DOWNLOAD" in result.stdout, result.stderr
    assert "starting over" in result.stderr


def test_a_finished_install_still_refuses_a_second_install(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / ".env").write_text("DOMAIN=bot.example.com\nPAURUBOT_IMAGE=ghcr.io/x@sha256:1\n")
    result = _run(tmp_path, STUB_INSTALL)
    assert result.returncode != 0 and "already installed" in result.stderr
    assert "REACHED_DOWNLOAD" not in result.stdout
