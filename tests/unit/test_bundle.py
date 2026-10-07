"""Copying a shop's setup to another install with a bundle file (FIN-07)."""

from __future__ import annotations

import json

import httpx
from sqlalchemy import func, select

from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.core import bundle
from paurubot.core import settings as shop_settings
from paurubot.db.models import BotText, Category, DiscountCode, Location, PanelKind, Plan, StaffRole
from tests import factories


async def _source(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Source")
    loc = Location(shop_id=shop.id, title="Germany", flag="🇩🇪")
    cat = Category(shop_id=shop.id, title="Monthly")
    session.add_all([loc, cat])
    await session.flush()
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    panel.name = "DE-1"
    plan = await factories.plan(session, shop, panel, price=120_000, title="Gold")
    plan.category_id, plan.location_id = cat.id, loc.id
    plan.panel_options = {"inbounds": ["vless:VLESS TCP"]}
    session.add(DiscountCode(shop_id=shop.id, code="SPRING", value=15, plan_ids=[plan.id], used_count=7))
    session.add(BotText(shop_id=shop.id, locale="en", key="welcome", value="Hi {name}!"))
    await shop_settings.set_many(
        session,
        shop.id,
        {"trial_days": 3, "zarinpal_merchant": "secret-merchant", "trial_panel_id": panel.id},
    )
    await session.flush()
    return shop


async def test_round_trip_without_secrets(session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(session)
    data = await bundle.export(session, source.id)
    text = json.dumps(data, ensure_ascii=False, default=str)
    assert "secret-merchant" not in text and "panel-password" not in text
    assert data["settings"]["trial_days"] == 3 and data["setting_refs"] == {"trial_panel_id": "DE-1"}

    target = await factories.shop(session, "Target")
    panel = await factories.panel(session, target, PanelKind.PASARGUARD)
    panel.name = "DE-1"
    await session.flush()
    first = await bundle.apply(session, target.id, json.loads(text))
    assert first.created == {
        "locations": 1,
        "categories": 1,
        "plans": 1,
        "discount_codes": 1,
        "bot_texts": 1,
    }
    plan = await session.scalar(select(Plan).where(Plan.shop_id == target.id))
    assert plan is not None and plan.panel_id == panel.id and plan.price == 120_000
    code = await session.scalar(select(DiscountCode).where(DiscountCode.shop_id == target.id))
    assert code is not None and code.plan_ids == [plan.id] and code.used_count == 0
    cfg = await shop_settings.load(session, target.id, fresh=True)
    assert cfg["trial_days"] == 3 and cfg["trial_panel_id"] == panel.id and cfg["zarinpal_merchant"] == ""
    again = await bundle.apply(session, target.id, json.loads(text))
    assert again.created == {} and set(again.updated) <= {"settings"}


async def test_missing_panel_falls_back_to_location_and_bad_files_are_refused(session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(session)
    data = await bundle.export(session, source.id)
    target = await factories.shop(session, "Elsewhere")
    report = await bundle.apply(session, target.id, data)
    plan = await session.scalar(select(Plan).where(Plan.shop_id == target.id))
    assert (
        plan is not None
        and plan.panel_id is None
        and plan.location_id is not None
        and plan.panel_options == {}
    )
    assert any("DE-1" in w for w in report.warnings)
    for bad in (
        {"format": "x"},
        {"format": bundle.FORMAT, "version": 99},
        {"format": bundle.FORMAT, "plans": "x"},
    ):
        try:
            await bundle.apply(session, target.id, bad)
        except bundle.BundleError:
            continue
        raise AssertionError(bad)


async def test_dry_run_through_the_api_changes_nothing(session) -> None:  # type: ignore[no-untyped-def]
    source = await _source(session)
    await create_staff(
        session, source.id, role=StaffRole.OWNER, username="bundler", password="a-very-strong-pass"
    )
    await session.commit()
    app = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=app, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login", json={"username": "bundler", "password": "a-very-strong-pass"}
        )
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        exported = await client.get("/api/admin/bundle")
        assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        data = exported.json()
        data["locations"].append({"title": "Finland"})
        files = {"file": ("b.json", json.dumps(data), "application/json")}
        dry = await client.post("/api/admin/bundle?dry_run=true", files=files, headers=csrf)
        assert dry.status_code == 200 and dry.json()["created"] == {"locations": 1}
        count = await session.scalar(select(func.count(Location.id)).where(Location.shop_id == source.id))
        assert count == 1
        real = await client.post("/api/admin/bundle?dry_run=false", files=files, headers=csrf)
        assert real.json()["created"] == {"locations": 1}
        bad = await client.post(
            "/api/admin/bundle", files={"file": ("b.json", "nope", "application/json")}, headers=csrf
        )
        assert bad.status_code == 422
