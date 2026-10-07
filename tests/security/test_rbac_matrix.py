"""The code matrix must match docs/AUTHZ.md exactly (SEC-15)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from paurubot.auth.rbac import PERMISSIONS, allowed, permissions_for
from paurubot.db.models import StaffRole

pytestmark = pytest.mark.security

DOC = Path(__file__).resolve().parents[2] / "docs" / "AUTHZ.md"
ROLES = [
    StaffRole.OWNER,
    StaffRole.ADMIN,
    StaffRole.FINANCE,
    StaffRole.SUPPORT,
    StaffRole.RESELLER,
    StaffRole.MERCHANT,
]


def _doc_matrix() -> dict[str, list[bool]]:
    rows: dict[str, list[bool]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*`([a-z.]+)`\s*\|(.+)\|\s*$", line)
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        rows[match.group(1)] = [cell in {"✅", "🔸"} for cell in cells]
    return rows


def test_doc_lists_every_permission() -> None:
    assert sorted(_doc_matrix()) == sorted(PERMISSIONS)


@pytest.mark.parametrize("permission", PERMISSIONS)
def test_code_matches_doc(permission: str) -> None:
    expected = _doc_matrix()[permission]
    actual = [allowed(role, permission) for role in ROLES]
    assert actual == expected, permission


def test_unknown_permission_and_role_are_denied() -> None:
    assert not allowed(StaffRole.OWNER, "everything.please")
    assert not allowed("superuser", "dashboard.view")


def test_reseller_allow_list_only_narrows() -> None:
    assert allowed(StaffRole.RESELLER, "services.create", ["services.create"])
    assert not allowed(StaffRole.RESELLER, "services.create", ["services.read"])
    # cannot widen beyond the role row
    assert not allowed(StaffRole.RESELLER, "wallet.adjust", ["wallet.adjust"])
    assert permissions_for(StaffRole.RESELLER, []) == []
