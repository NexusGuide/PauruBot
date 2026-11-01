"""SEC-F24: production dependencies come from one pinned, hash-checked lock.

`requirements.txt` holds ranges, so two builds could install different code. The image and CI
now install only `requirements.lock`, generated for every supported Python, with --require-hashes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]


def _pins() -> dict[str, tuple[str, int]]:
    """name -> (version, number of hashes) from the lock."""
    pins: dict[str, tuple[str, int]] = {}
    current: str | None = None
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", stripped)
        if match:
            current = canonicalize_name(match.group(1))
            pins[current] = (match.group(2), 0)
        elif stripped.startswith("--hash=sha256:") and current is not None:
            version, count = pins[current]
            pins[current] = (version, count + 1)
    return pins


def _requirements() -> list[Requirement]:
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    return [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]


def test_every_locked_package_is_pinned_and_hashed() -> None:
    pins = _pins()
    assert len(pins) > 20
    assert all(count >= 1 for _, count in pins.values()), [n for n, (_, c) in pins.items() if not c]


def test_lock_covers_and_satisfies_every_requirement() -> None:
    pins = _pins()
    for requirement in _requirements():
        name = canonicalize_name(requirement.name)
        assert name in pins, f"{name} is missing from requirements.lock; regenerate it"
        assert requirement.specifier.contains(pins[name][0], prereleases=True), (name, pins[name][0])


def test_pyproject_dependencies_are_all_in_requirements() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {canonicalize_name(Requirement(dep).name) for dep in project["dependencies"]}
    listed = {canonicalize_name(req.name) for req in _requirements()}
    assert declared <= listed, declared - listed


def test_image_and_ci_install_only_the_hashed_lock() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "pip wheel --require-hashes --wheel-dir /wheels -r requirements.lock" in dockerfile
    assert "-r requirements.txt" not in dockerfile
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "pip install --require-hashes -r requirements.lock" in ci
    assert "-r requirements.txt" not in ci
    # CI tests the Python and Node the image ships.
    python_image = re.search(r"FROM python:(\d+\.\d+)-slim", dockerfile)
    node_image = re.search(r"FROM node:(\d+)-alpine", dockerfile)
    assert python_image and f'"{python_image.group(1)}"' in ci
    assert node_image and f'node-version: "{node_image.group(1)}"' in ci
