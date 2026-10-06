"""Admin API authorization: the role matrix, reseller scoping, tenant isolation and secret handling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from paurubot.api.app import create_app
from paurubot.auth import rbac
from paurubot.auth.staff import create_staff
from paurubot.db.models import (
    AuditLog,
    Panel,
    ResellerProfile,
    Service,
    ServiceStatus,
    Shop,
    StaffRole,
)
from paurubot.ledger.accounts import wallet_balance
from tests import factories

pytestmark = pytest.mark.security
PASSWORD = "a-very-strong-pass"


@dataclass
class Env:
    shop: Shop
    other_shop: Shop
    panel: Panel
    other_panel: Panel
    customer_id: int
    reseller_customer_id: int
    reseller_service_id: int
    other_service_id: int


@pytest.fixture
async def env(session) -> Env:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Main")
    other = await factories.shop(session, "Other")
    panel = await factories.panel(session, shop)
    other_panel = await factories.panel(session, other)
    customer = await factories.customer(session, shop)
    reseller_customer = await factories.customer(session, shop, username="reseller")
    profile = ResellerProfile(
        shop_id=shop.id, customer_id=reseller_customer.id, permissions=sorted(rbac.SCOPED_FOR_RESELLER)
    )
    session.add(profile)
    await session.flush()
    for role in StaffRole:
        await create_staff(
            session,
            shop.id,
            role=role,
            username=f"u_{role.value}",
            password=PASSWORD,
            reseller_profile_id=profile.id if role == StaffRole.RESELLER else None,
        )
    await create_staff(session, other.id, role=StaffRole.OWNER, username="other_owner", password=PASSWORD)
    mine = Service(
        shop_id=shop.id,
        customer_id=reseller_customer.id,
        panel_id=panel.id,
        panel_username="r_1",
        status=ServiceStatus.ACTIVE,
    )
    theirs = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="c_1",
        status=ServiceStatus.ACTIVE,
    )
    session.add_all([mine, theirs])
    await session.commit()
    return Env(shop, other, panel, other_panel, customer.id, reseller_customer.id, mine.id, theirs.id)


@pytest.fixture
async def http(session) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=create_app(), client=("10.9.8.7", 4444))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


class Staff:
    """A logged-in browser session for one staff account."""

    def __init__(self, client: httpx.AsyncClient, csrf: str) -> None:
        self.client, self.csrf = client, csrf

    async def call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        headers = {"x-csrf-token": self.csrf}
        return await self.client.request(method, path, json=body, headers=headers)


async def login(http: httpx.AsyncClient, username: str) -> Staff:
    http.cookies.clear()
    resp = await http.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return Staff(http, resp.json()["csrf_token"])


def endpoints(env: Env) -> list[tuple[str, str, str, Any]]:
    """(permission, method, path, body). Bodies are valid, so an allowed call never answers 403."""
    cid = env.customer_id
    return [
        ("dashboard.view", "GET", "/api/admin/dashboard", None),
        ("settings.read", "GET", "/api/admin/settings", None),
        ("settings.write", "PUT", "/api/admin/settings", {"values": {"min_topup": 30000}}),
        ("panels.read", "GET", "/api/admin/panels", None),
        ("panels.secrets", "POST", f"/api/admin/panels/{env.panel.id}/webhook", None),
        ("catalog.read", "GET", "/api/admin/catalog", None),
        ("catalog.write", "POST", "/api/admin/locations", {"title": "DE"}),
        ("customers.read", "GET", "/api/admin/customers", None),
        ("customers.ban", "POST", f"/api/admin/customers/{cid}/ban", {"banned": False}),
        ("wallet.adjust", "POST", f"/api/admin/customers/{cid}/adjust", {"delta": 1000, "memo": "gift"}),
        ("services.read", "GET", "/api/admin/services", None),
        (
            "services.write",
            "POST",
            "/api/admin/services/bulk",
            {"action": "disable", "ids": [env.reseller_service_id]},
        ),
        ("orders.read", "GET", "/api/admin/orders", None),
        ("payments.read", "GET", "/api/admin/payments", None),
        ("discounts.write", "GET", "/api/admin/discounts", None),
        ("broadcast.send", "GET", "/api/admin/broadcasts/audience", None),
        ("texts.write", "GET", "/api/admin/texts", None),
        ("tickets.read", "GET", "/api/admin/tickets", None),
        ("reports.read", "GET", "/api/admin/reports/sales", None),
        ("audit.read", "GET", "/api/admin/audit", None),
        ("staff.manage", "GET", "/api/admin/staff", None),
        ("resellers.manage", "GET", "/api/admin/resellers", None),
        ("backups.manage", "GET", "/api/admin/backups", None),
        ("resellers.sub", "GET", "/api/admin/sub-resellers", None),
    ]


@pytest.mark.parametrize("role", [r for r in StaffRole])
async def test_every_route_follows_the_role_matrix(http, env, role) -> None:  # type: ignore[no-untyped-def]
    staff = await login(http, f"u_{role.value}")
    for permission, method, path, body in endpoints(env):
        granted = rbac.allowed(role, permission)  # the reseller profile allows its whole role row
        resp = await staff.call(method, path, body)
        if granted:
            assert resp.status_code != 403, (role, permission, resp.text)
            assert resp.status_code < 500, (role, permission, resp.text)
        else:
            assert resp.status_code == 403, (role, permission, resp.status_code)
            assert resp.json()["error"] == "forbidden"


async def test_mutations_need_the_csrf_header(http, env) -> None:  # type: ignore[no-untyped-def]
    staff = await login(http, "u_owner")
    resp = await http.post("/api/admin/locations", json={"title": "NL"})
    assert resp.status_code == 403 and resp.json()["error"] == "csrf_failed"
    assert (await staff.call("POST", "/api/admin/locations", {"title": "NL"})).status_code == 200


async def test_other_shops_rows_are_invisible(http, env) -> None:  # type: ignore[no-untyped-def]
    staff = await login(http, "other_owner")
    assert (await staff.call("GET", f"/api/admin/customers/{env.customer_id}")).status_code == 404
    assert (await staff.call("GET", f"/api/admin/services/{env.other_service_id}")).status_code == 404
    assert (await staff.call("POST", f"/api/admin/panels/{env.panel.id}/test")).status_code == 404
    assert (await staff.call("GET", f"/api/admin/plans/panel-options/{env.panel.id}")).status_code == 404
    listed = (await staff.call("GET", "/api/admin/panels")).json()["items"]
    assert [p["id"] for p in listed] == [env.other_panel.id]
    adjust = await staff.call(
        "POST", f"/api/admin/customers/{env.customer_id}/adjust", {"delta": 5000, "memo": "steal"}
    )
    assert adjust.status_code == 404


async def test_reseller_only_sees_its_own_services(http, env) -> None:  # type: ignore[no-untyped-def]
    staff = await login(http, "u_reseller")
    listed = (await staff.call("GET", "/api/admin/services")).json()["items"]
    assert [s["id"] for s in listed] == [env.reseller_service_id]
    assert (await staff.call("GET", f"/api/admin/services/{env.other_service_id}")).status_code == 404
    assert (await staff.call("GET", f"/api/admin/services/{env.reseller_service_id}")).status_code == 200
    assert (await staff.call("GET", "/api/admin/customers")).json()["items"] == []


async def test_reseller_allow_list_narrows_the_role(session, http, env) -> None:  # type: ignore[no-untyped-def]
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.shop_id == env.shop.id))
    profile.permissions = ["dashboard.view", "services.read"]
    await session.commit()
    staff = await login(http, "u_reseller")
    assert (await staff.call("GET", "/api/admin/services")).status_code == 200
    assert (await staff.call("GET", "/api/admin/orders")).status_code == 403
    me = (await http.get("/api/auth/me")).json()
    assert set(me["permissions"]) == {"dashboard.view", "services.read"}


async def test_panel_secrets_are_write_only_and_owner_only(session, http, env) -> None:  # type: ignore[no-untyped-def]
    owner = await login(http, "u_owner")
    created = await owner.call(
        "POST",
        "/api/admin/panels",
        {
            "name": "pg",
            "kind": "pasarguard",
            "base_url": "https://pg.example.com",
            "username": "root",
            "password": "panel-secret-123",
            "api_token": "tok-secret-456",
        },
    )
    assert created.status_code == 200, created.text
    text = created.text + (await owner.call("GET", "/api/admin/panels")).text
    assert "panel-secret-123" not in text and "tok-secret-456" not in text
    assert created.json()["has_api_token"] is True
    panel_id = created.json()["id"]

    admin = await login(http, "u_admin")
    base = {"name": "pg2", "kind": "pasarguard", "base_url": "https://pg.example.com", "username": "root"}
    assert (await admin.call("PUT", f"/api/admin/panels/{panel_id}", base)).status_code == 200
    stolen = await admin.call("PUT", f"/api/admin/panels/{panel_id}", {**base, "password": "x" * 12})
    assert stolen.status_code == 403
    moved = await admin.call(
        "PUT", f"/api/admin/panels/{panel_id}", {**base, "base_url": "https://evil.example"}
    )
    assert moved.status_code == 403
    private = await admin.call(
        "PUT", f"/api/admin/panels/{panel_id}", {**base, "allow_private_address": True}
    )
    assert private.status_code == 403
    secret_setting = await admin.call("PUT", "/api/admin/settings", {"values": {"zarinpal_merchant": "m"}})
    assert secret_setting.status_code == 403
    settings = (await admin.call("GET", "/api/admin/settings")).json()["groups"]["payments"]
    merchant = next(item for item in settings if item["key"] == "zarinpal_merchant")
    assert merchant["value"] == {"set": False}


async def test_unsafe_panel_addresses_are_refused(http, env) -> None:  # type: ignore[no-untyped-def]
    owner = await login(http, "u_owner")
    for url in ["http://169.254.169.254/latest", "https://localhost:8000"]:
        resp = await owner.call(
            "POST",
            "/api/admin/panels",
            {"name": "x", "kind": "marzban", "base_url": url, "username": "a", "password": "p" * 12},
        )
        assert resp.status_code == 422, (url, resp.text)


async def test_wallet_adjust_is_idempotent_and_audited(session, http, env) -> None:  # type: ignore[no-untyped-def]
    finance = await login(http, "u_finance")
    body = {"delta": 7000, "memo": "compensation", "request_id": "req-1"}
    first = await finance.call("POST", f"/api/admin/customers/{env.customer_id}/adjust", body)
    again = await finance.call("POST", f"/api/admin/customers/{env.customer_id}/adjust", body)
    assert first.status_code == 200 and again.status_code == 200
    assert await wallet_balance(session, env.shop.id, env.customer_id) == 7000
    zero = await finance.call(
        "POST", f"/api/admin/customers/{env.customer_id}/adjust", {"delta": 0, "memo": "x"}
    )
    assert zero.status_code == 422
    entries = (await session.scalars(select(AuditLog).where(AuditLog.shop_id == env.shop.id))).all()
    assert any(a.action.startswith("wallet") for a in entries)


async def test_staff_management_guards(http, env) -> None:  # type: ignore[no-untyped-def]
    owner = await login(http, "u_owner")
    owner_again = await owner.call(
        "POST", "/api/admin/staff", {"role": "owner", "username": "second", "password": PASSWORD}
    )
    assert owner_again.status_code == 422
    made = await owner.call(
        "POST", "/api/admin/staff", {"role": "support", "username": "helper", "password": PASSWORD}
    )
    assert made.status_code == 200 and "password" not in made.text and "password_hash" not in made.json()
    me = (await http.get("/api/auth/me")).json()
    demote_self = await owner.call("PATCH", f"/api/admin/staff/{me['id']}", {"role": "support"})
    assert demote_self.status_code == 422

    helper = await login(http, "helper")
    assert (await helper.call("GET", "/api/admin/customers")).status_code == 200
    owner = await login(http, "u_owner")
    await owner.call("PATCH", f"/api/admin/staff/{made.json()['id']}", {"is_active": False})
    resp = await http.post("/api/auth/login", json={"username": "helper", "password": PASSWORD})
    assert resp.status_code in {401, 403}


async def test_texts_are_validated_and_reset(http, env) -> None:  # type: ignore[no-untyped-def]
    admin = await login(http, "u_admin")
    items = (await admin.call("GET", "/api/admin/texts")).json()["items"]
    key = next(i["key"] for i in items if "name" in i["variables"])
    bad = await admin.call("PUT", f"/api/admin/texts/fa/{key}", {"value": "hi {name.__class__}"})
    assert bad.status_code == 422
    unknown = await admin.call("PUT", f"/api/admin/texts/fa/{key}", {"value": "hi {secret_var}"})
    assert unknown.status_code == 422
    ok = await admin.call("PUT", f"/api/admin/texts/en/{key}", {"value": "Hello {name}"})
    assert ok.status_code == 200
    assert (await admin.call("PUT", "/api/admin/texts/en/no_such_key", {"value": "x"})).status_code == 404
    reset = await admin.call("DELETE", f"/api/admin/texts/en/{key}")
    assert reset.status_code == 200 and reset.json()["value"] != "Hello {name}"


async def test_sales_report_and_csv(http, env) -> None:  # type: ignore[no-untyped-def]
    finance = await login(http, "u_finance")
    report = await finance.call("GET", "/api/admin/reports/sales")
    assert report.status_code == 200 and report.json()["totals"] == {"orders": 0, "amount": 0}
    csv_resp = await finance.call("GET", "/api/admin/reports/sales.csv?start=2026-01-01&end=2026-01-31")
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv")
    bad = await finance.call("GET", "/api/admin/reports/sales?start=2026-02-01&end=2026-01-01")
    assert bad.status_code == 422


async def test_level_one_reseller_sees_its_sub_resellers(http, env, session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Customer

    reseller = await login(http, "u_reseller")
    ids = lambda resp: {s["id"] for s in resp.json()["items"]}  # noqa: E731
    assert ids(await reseller.call("GET", "/api/admin/services")) == {env.reseller_service_id}

    customer = await session.get(Customer, env.customer_id)
    added = await reseller.call(
        "POST", "/api/admin/sub-resellers", {"telegram_id": customer.telegram_id, "discount_percent": 0}
    )
    assert added.status_code == 200, added.text
    too_high = await reseller.call(
        "PUT", f"/api/admin/sub-resellers/{added.json()['id']}", {"discount_percent": 50, "is_active": True}
    )
    assert too_high.status_code == 422 and too_high.json()["error"] == "subreseller_discount_too_high"
    listed = await reseller.call("GET", "/api/admin/sub-resellers")
    assert [r["customer_id"] for r in listed.json()["items"]] == [env.customer_id]
    # The level-2 reseller's services are now in scope; nothing else is.
    assert ids(await reseller.call("GET", "/api/admin/services")) == {
        env.reseller_service_id,
        env.other_service_id,
    }
    assert (await reseller.call("GET", "/api/admin/customers")).json()["total"] == 0

    owner = await login(http, "u_owner")
    assert (await owner.call("GET", "/api/admin/sub-resellers")).status_code == 404


async def test_reseller_writes_to_the_shop_from_the_panel(http, env) -> None:  # type: ignore[no-untyped-def]
    reseller = await login(http, "u_reseller")
    opened = await reseller.call("POST", "/api/admin/tickets", {"subject": "Payout", "text": "Please settle"})
    assert opened.status_code == 200, opened.text
    ticket_id = opened.json()["id"]
    assert (
        await reseller.call("POST", f"/api/admin/tickets/{ticket_id}/reply", {"text": "Any news?"})
    ).status_code == 200
    detail = (await reseller.call("GET", f"/api/admin/tickets/{ticket_id}")).json()
    assert detail["ticket"]["source"] == "panel"
    assert [m["author_type"] for m in detail["messages"]] == ["customer", "customer"]

    owner = await login(http, "u_owner")
    assert (await owner.call("POST", "/api/admin/tickets", {"subject": "x", "text": "y"})).status_code == 404
    assert (
        await owner.call("POST", f"/api/admin/tickets/{ticket_id}/reply", {"text": "Done"})
    ).status_code == 200
    detail = (await owner.call("GET", f"/api/admin/tickets/{ticket_id}")).json()
    assert detail["messages"][-1]["author_type"] == "staff"
