"""AUD-1: the installer fetches what this repository builds and signs.

install.sh named another GitHub account for the source, the release images, the base-image
mirror and the cosign identity, while the README sends operators to this repository and the
release workflow publishes under this repository's owner. A fresh install or update then could
not get this project's verified images at all, and whoever controls the other account decided
what every install ran.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _repo_from_readme() -> str:
    found = {
        match
        for name in ("README.md", "README.en.md")
        for match in re.findall(
            r"raw\.githubusercontent\.com/([\w.-]+/[\w.-]+)/main/install\.sh", (ROOT / name).read_text()
        )
    }
    assert len(found) == 1, found
    return found.pop()


def _assigned(text: str, name: str) -> str:
    match = re.search(rf'^\s*(?:local\s+)?{name}="([^"]+)"', text, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_the_installer_uses_this_repository_and_its_registry() -> None:
    repo = _repo_from_readme()
    owner = repo.split("/")[0].lower()
    installer = (ROOT / "install.sh").read_text()
    assert _assigned(installer, "REPO") == repo
    assert _assigned(installer, "IMAGE_REPO") == f"ghcr.io/{owner}/paurubot"
    mirror = re.search(r'local mirror="([^"]+)"', installer)
    assert mirror and mirror.group(1) == f"ghcr.io/{owner}/paurubot-mirror"
    # The one-liner in the installer's own header and the CLI reference say the same.
    for text in (installer, (ROOT / "docs" / "reference" / "cli.md").read_text()):
        assert f"raw.githubusercontent.com/{repo}/main/install.sh" in text


def test_the_release_workflow_publishes_where_the_installer_looks() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # Images and mirrors go under the repository owner, lower-cased, and are signed by this
    # workflow; the installer's cosign identity is built from REPO, so the two must agree.
    assert "IMAGE: ghcr.io/${{ github.repository_owner }}/paurubot" in release
    assert 'echo "IMAGE=${IMAGE,,}"' in release
    assert 'target="ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/paurubot-mirror"' in release
    installer = (ROOT / "install.sh").read_text()
    assert "identity_repo=${REPO//./\\\\.}" in installer
    assert "\\.github/workflows/release\\.yml@refs/" in installer


def test_a_manual_release_is_checked_before_anything_is_published() -> None:
    """A release can also be a manual run on main: it tags the commit it built itself, but only after
    checking the version against pyproject.toml and that the tag does not exist yet."""
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    check = release.index("Check the version being released")
    assert check < release.index("docker/build-push-action")  # before any image is pushed
    for guard in ("pyproject.toml", "refs/heads/main", "git/ref/tags/${RELEASE}"):
        assert guard in release[check : release.index("docker/setup-qemu-action")], guard
    publish = release[release.index("Publish the release") :]
    assert 'ref="refs/tags/${RELEASE}" -f sha="${GITHUB_SHA}"' in publish
    assert "--verify-tag" in publish


def test_dev_channel_is_wired_across_workflows_and_installer() -> None:
    import subprocess

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    installer = (ROOT / "install.sh").read_text()
    assert "branches: [main, dev]" in ci
    assert "branches: [main, dev]" in release
    assert "type=raw,value=dev,enable=${{ github.ref == 'refs/heads/dev' && env.RELEASE == '' }}" in release
    assert "heads/(main|dev)|tags/v[^/]+" in installer
    assert "^(stable|beta|dev)?$" in installer
    match = re.search(r"^resolve_target\(\) \{\n.*?^\}", installer, re.S | re.M)
    assert match
    script = (
        "set -Eeuo pipefail\nIMAGE_REPO=ghcr.io/nexusguide/paurubot\n"
        'die() { echo "$*" >&2; exit 19; }\n'
        + match.group(0)
        + '\nresolve_target dev ""\nprintf "%s %s\\n" "$TARGET_REF" "$TARGET_IMAGE"\n'
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "dev ghcr.io/nexusguide/paurubot:dev"


def test_the_cli_exposes_channel_and_version_management() -> None:
    installer = (ROOT / "install.sh").read_text()
    # The menu and the non-interactive CLI both expose the switch clearly.
    assert "14) Channels / versions" in installer
    assert "channels | releases) cmd_channels" in installer
    assert "versions) cmd_versions" in installer
    assert "channel | switch) cmd_switch" in installer
    assert "Choose a release:" in installer
    assert "a specific stable version" in installer
    assert "pauru switch stable|beta|dev" in installer
    assert "pauru switch --version vX.Y.Z" in installer
    assert "PAURUBOT_PRE_DEV_BACKUP" in installer  # beta and dev share the pre-release snapshot
