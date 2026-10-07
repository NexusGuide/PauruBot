"""Role permission matrix (SEC-15). docs/AUTHZ.md is the human-readable contract for this table."""

from __future__ import annotations

from collections.abc import Iterable

from paurubot.db.models import StaffRole

PERMISSIONS: tuple[str, ...] = (
    "dashboard.view",
    "settings.read",
    "settings.write",
    "panels.read",
    "panels.write",
    "panels.secrets",
    "catalog.read",
    "catalog.write",
    "customers.read",
    "customers.write",
    "customers.ban",
    "wallet.adjust",
    "services.read",
    "services.write",
    "services.create",
    "orders.read",
    "payments.read",
    "payments.review",
    "payments.refund",
    "discounts.write",
    "broadcast.send",
    "texts.write",
    "tickets.read",
    "tickets.reply",
    "reports.read",
    "audit.read",
    "staff.manage",
    "resellers.manage",
    "resellers.sub",
    "resellers.bot",
    "backups.manage",
)

_ALL = frozenset(PERMISSIONS)

MATRIX: dict[StaffRole, frozenset[str]] = {
    StaffRole.OWNER: _ALL,
    StaffRole.ADMIN: _ALL - {"panels.secrets", "payments.refund", "staff.manage", "backups.manage"},
    StaffRole.FINANCE: frozenset(
        {
            "dashboard.view",
            "catalog.read",
            "customers.read",
            "wallet.adjust",
            "services.read",
            "orders.read",
            "payments.read",
            "payments.review",
            "payments.refund",
            "reports.read",
        }
    ),
    StaffRole.SUPPORT: frozenset(
        {
            "dashboard.view",
            "catalog.read",
            "customers.read",
            "customers.write",
            "customers.ban",
            "services.read",
            "services.write",
            "orders.read",
            "payments.read",
            "tickets.read",
            "tickets.reply",
        }
    ),
    StaffRole.RESELLER: frozenset(
        {
            "dashboard.view",
            "catalog.read",
            "customers.read",
            "customers.write",
            "services.read",
            "services.write",
            "services.create",
            "orders.read",
            "tickets.read",
            "tickets.reply",
            "reports.read",
            "resellers.sub",
            "resellers.bot",
        }
    ),
    # A reseller's own bot shop (TEN-04): day-to-day selling there, nothing about the platform.
    StaffRole.MERCHANT: frozenset(
        {
            "dashboard.view",
            "customers.read",
            "customers.write",
            "customers.ban",
            "wallet.adjust",
            "services.read",
            "orders.read",
            "payments.read",
            "payments.review",
            "payments.refund",
            "broadcast.send",
            "texts.write",
            "tickets.read",
            "tickets.reply",
            "reports.read",
        }
    ),
}

# Permissions a reseller only ever holds for its own customers and services.
SCOPED_FOR_RESELLER = MATRIX[StaffRole.RESELLER]
# What an approved reseller gets by default; sub-resellers are granted one by one.
DEFAULT_FOR_RESELLER = SCOPED_FOR_RESELLER - {"resellers.sub", "resellers.bot"}


def allowed(role: StaffRole | str, permission: str, reseller_allow_list: Iterable[str] | None = None) -> bool:
    """Deny by default. Reseller rights are the role row narrowed by the profile's allow-list."""
    if permission not in _ALL:
        return False
    try:
        role = StaffRole(role)
    except ValueError:
        return False
    granted = MATRIX.get(role, frozenset())
    if permission not in granted:
        return False
    if role == StaffRole.RESELLER and reseller_allow_list is not None:
        narrowed = set(reseller_allow_list)
        return permission in narrowed
    return True


def permissions_for(role: StaffRole | str, reseller_allow_list: Iterable[str] | None = None) -> list[str]:
    allow = list(reseller_allow_list) if reseller_allow_list is not None else None
    return [perm for perm in PERMISSIONS if allowed(role, perm, allow)]


def is_scoped(role: StaffRole | str) -> bool:
    return StaffRole(role) == StaffRole.RESELLER
