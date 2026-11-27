"""Unit tests for admin customer panel-account visibility and order enrichment."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from paurubot.api.routes.admin.commerce import ACCOUNT_ORDER_KINDS, _order_details, enrich_orders
from paurubot.api.routes.admin.customers import _customer_order_out, _panel_account_out
from paurubot.core import panel_accounts
from paurubot.db.models import OrderKind


class _FakeDB:
    def __init__(
        self,
        *,
        account_plans: list[SimpleNamespace] | None = None,
        accounts: list[SimpleNamespace] | None = None,
        services: list[SimpleNamespace] | None = None,
        panels: list[tuple[int, str]] | None = None,
    ) -> None:
        self._ap = account_plans or []
        self._acc = accounts or []
        self._svc = services or []
        self._panels = panels or []

    async def scalars(self, stmt):  # type: ignore[no-untyped-def]
        sql = str(stmt)
        if "account_plans" in sql:
            return self._ap
        if "panel_accounts" in sql:
            return self._acc
        if "services" in sql:
            return self._svc
        return []

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        sql = str(stmt)
        if "panels" in sql:
            return self._panels
        return []


def test_account_order_kinds_matches_core_panel_accounts() -> None:
    assert set(ACCOUNT_ORDER_KINDS) == set(panel_accounts.KINDS)


def test_order_details_extracts_specs_safely() -> None:
    renew_order = SimpleNamespace(
        kind=OrderKind.ACCOUNT_RENEW,
        params={"renew": {"days": 30, "gb": 200, "package": 0}},
    )
    assert _order_details(renew_order) == {"days": 30, "gb": 200}

    gb_order = SimpleNamespace(kind=OrderKind.ACCOUNT_GB, params={"gb": 50})
    assert _order_details(gb_order) == {"gb": 50}

    days_order = SimpleNamespace(kind=OrderKind.ACCOUNT_DAYS, params={"days": 15})
    assert _order_details(days_order) == {"days": 15}

    users_order = SimpleNamespace(kind=OrderKind.ACCOUNT_USERS, params={"users": 10})
    assert _order_details(users_order) == {"users": 10}

    bad_order = SimpleNamespace(kind=OrderKind.ACCOUNT_GB, params={"gb": "oops"})
    assert _order_details(bad_order) == {}


@pytest.mark.asyncio
async def test_enrich_orders_resolves_account_plan_username_and_panel() -> None:
    ap = SimpleNamespace(id=7, title="Reseller Gold", panel_id=3)
    acc = SimpleNamespace(
        id=42,
        username="r1001_abc",
        plan=ap,
        panel=SimpleNamespace(name="Germany-PG"),
    )
    svc = SimpleNamespace(
        id=99,
        label="MyVPN",
        panel_username="u1001_xyz",
        panel=SimpleNamespace(name="Finland-01"),
    )
    orders = [
        SimpleNamespace(
            id=1,
            kind=OrderKind.ACCOUNT_PURCHASE,
            plan=None,
            service_id=None,
            params={"account_plan_id": 7, "account_id": 42, "username": "r1001_abc"},
        ),
        SimpleNamespace(
            id=2,
            kind=OrderKind.ACCOUNT_RENEW,
            plan=None,
            service_id=None,
            params={"account_plan_id": 7, "account_id": 42, "renew": {"days": 30, "gb": 500}},
        ),
        SimpleNamespace(
            id=3,
            kind=OrderKind.PURCHASE,
            plan=SimpleNamespace(title="Monthly 50GB"),
            service_id=99,
            params={},
        ),
    ]
    db = _FakeDB(account_plans=[ap], accounts=[acc], services=[svc], panels=[(3, "Germany-PG")])
    meta = await enrich_orders(db, orders)

    assert meta[1]["plan"] == "Reseller Gold"
    assert meta[1]["account_id"] == 42
    assert meta[1]["account_username"] == "r1001_abc"
    assert meta[1]["panel"] == "Germany-PG"
    assert meta[1]["details"] == {}

    assert meta[2]["plan"] == "Reseller Gold"
    assert meta[2]["account_username"] == "r1001_abc"
    assert meta[2]["details"] == {"days": 30, "gb": 500}

    assert meta[3]["plan"] == "Monthly 50GB"
    assert meta[3]["service_username"] == "MyVPN"
    assert meta[3]["panel"] == "Finland-01"


def test_panel_account_out_and_customer_order_out() -> None:
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    customer = SimpleNamespace(staff_label="@reseller1", telegram_id=1001)
    account = SimpleNamespace(
        id=42,
        customer_id=5,
        panel_id=3,
        plan_id=7,
        username="r1001_abc",
        data_limit=100 * 1024**3,
        max_users=25,
        expire_at=now,
        status="active",
        used_bytes=10 * 1024**3,
        users_count=8,
        panel_status="active",
        synced_at=now,
        note="VIP reseller",
        created_at=now,
        auto_renew={"package": 0},
        plan=SimpleNamespace(title="Reseller Gold"),
        panel=SimpleNamespace(name="Germany-PG"),
    )
    row = _panel_account_out(account, customer)
    assert row["username"] == "r1001_abc"
    assert row["plan"] == "Reseller Gold"
    assert row["panel"] == "Germany-PG"
    assert row["auto_renew"] is True

    order = SimpleNamespace(
        id=10,
        kind=OrderKind.ACCOUNT_RENEW,
        status="completed",
        service_id=None,
        amount=500000,
        created_at=now,
        completed_at=now,
        error=None,
    )
    out = _customer_order_out(
        order,
        {10: {"plan": "Reseller Gold", "account_username": "r1001_abc", "details": {"days": 30}}},
    )
    assert out["plan"] == "Reseller Gold"
    assert out["account_username"] == "r1001_abc"
    assert out["details"] == {"days": 30}


def test_renew_packages_allow_zero_days_for_unlimited_time() -> None:
    pkgs = panel_accounts.clean_packages(
        [
            {"days": 30, "gb": 2000, "price": 600_000},
            {"days": 0, "gb": 1000, "price": 600_000},
            {"days": 0, "gb": 2000, "price": 1_200_000},
        ]
    )
    assert pkgs == [
        {"days": 30, "gb": 2000, "price": 600_000},
        {"days": 0, "gb": 1000, "price": 600_000},
        {"days": 0, "gb": 2000, "price": 1_200_000},
    ]
    with pytest.raises(ValueError, match="renew_packages_invalid"):
        panel_accounts.clean_packages([{"days": -1, "gb": 1000, "price": 600_000}])
