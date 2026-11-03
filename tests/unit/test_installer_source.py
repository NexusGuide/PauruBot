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
