"""Manual file/bundle ingestion and explicit national-network delivery regressions."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.api.routes import webapp_stock
from paurubot.api.routes.admin import manual_sales
from paurubot.core import catalog, manual_stock, national, orders, stock
from paurubot.db.base import Base
from paurubot.db.models import Order, OrderKind, OrderStatus, Plan, Shop, StockItem
from paurubot.ledger import wallet
from paurubot.panels import PanelUnavailable
from paurubot.panels.registry import driver_for
from paurubot.provisioning import lifecycle
from tests import factories


@pytest.mark.parametrize(
    "values",
    [
        {"text": ""},
        {"text": "x", "filename": "../config.txt"},
        {"text": "x", "filename": "x.html"},
        {"text": "x", "filename": "x.exe"},
        {"text": "x", "filename": "x\u202etxt.conf"},
        {"data": "not-base64", "filename": "x.conf"},
        {"data": "YQ=="},
        {"text": "a", "data": "YQ==", "filename": "x.conf"},
        {"text": "x", "subscription_url": "javascript:alert(1)"},
        {"text": "x", "subscription_url": "https://user:pass@example.com"},
        {"text": "x", "subscription_url": "https://example.com/\nsecret"},
        {"text": "x" * (manual_stock.MAX_FILE + 1)},
    ],
)
def test_part_rejects_unsafe_content(values):
    with pytest.raises(ValidationError):
        manual_stock.Part(**values)


def test_binary_and_legacy_roundtrip():
    blob = b"\x00\xffwireguard\n"
    part = manual_stock.Part(data=base64.b64encode(blob).decode(), filename="tunnel.conf")
    assert manual_stock.decode(manual_stock.encode([part]))[0].content() == blob
    assert manual_stock.decode("vless://old")[0].text == "vless://old"
    assert manual_stock.display(manual_stock.encode([part])) == "[file: tunnel.conf]"


@pytest.mark.parametrize("mode,number", [("single", 1), ("bundle", 1), ("bulk", 2)])
async def test_import_modes_encrypted_and_deduplicated(session, mode, number):
    shop = await factories.shop(session)
    plan = Plan(shop_id=shop.id, title="Ready", delivery="stock")
    session.add(plan)
    await session.flush()
    parts = [manual_stock.Part(text="vless://one")]
    if mode != "single":
        parts.append(manual_stock.Part(text="vless://two"))
    body = manual_stock.Import(plan_id=plan.id, name="Ready", mode=mode, parts=parts)
    assert (await manual_stock.add(session, plan, body))["added"] == number
    assert (await manual_stock.add(session, plan, body))["duplicates"] == number
    items = list(await session.scalars(select(StockItem).where(StockItem.plan_id == plan.id)))
    assert len(items) == number
    assert all("vless://" not in i.payload_enc for i in items)
    assert sum(len(manual_stock.parts(i)) for i in items) == len(parts)
    if mode == "single":
        assert (await stock.add(session, plan, "vless://one")).duplicates == 1


async def test_quota_includes_legacy_and_prevents_overflow(session, monkeypatch):
    shop = await factories.shop(session)
    plan = Plan(shop_id=shop.id, title="Ready", delivery="stock")
    session.add(plan)
    await session.flush()
    await stock.add(session, plan, "old-config")
    assert await manual_stock.storage_used(session, shop.id) > 0
    monkeypatch.setattr(manual_stock, "STORAGE_LIMIT", 1)
    with pytest.raises(stock.StockError, match="stock_storage_full"):
        await manual_stock.add(
            session,
            plan,
            manual_stock.Import(plan_id=plan.id, name="x", parts=[manual_stock.Part(text="new")]),
        )


async def national_env(session):
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    target = Plan(shop_id=shop.id, title="Replacement", delivery="stock", price=plan.price)
    session.add(target)
    await session.flush()
    plan.national_stock_plan_id = target.id
    panel.national_mode = True
    await manual_stock.add(
        session,
        target,
        manual_stock.Import(
            plan_id=target.id, name="Emergency", parts=[manual_stock.Part(text="vless://replacement")]
        ),
    )
    customer = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, customer.id, 500_000, provider="card", payment_id=customer.id)
    await session.flush()
    return shop, panel, plan, target, customer


async def test_national_purchase_reservation_and_fulfillment_without_panel(session, monkeypatch):
    shop, panel, plan, target, customer = await national_env(session)
    assert await catalog.get_plan(session, shop.id, plan.id, customer) is not None
    assert (await national.route(session, plan)).id == target.id
    with pytest.raises(PanelUnavailable, match="national_network_mode"):
        await driver_for(panel)
    order = await orders.create(
        session, customer, await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan)
    )
    assert order.params["stock_plan_id"] == target.id
    await orders.settle_from_wallet(session, order.id)
    item = await session.scalar(select(StockItem).where(StockItem.order_id == order.id))
    assert item.status == stock.RESERVED
    assert not await stock.for_customer(session, customer.id)

    async def no_op(*args, **kwargs):
        pass

    # Business post-sale hooks are orthogonal; actual delivery and queue run normally.
    monkeypatch.setattr(lifecycle, "after_sale", no_op)
    await lifecycle.fulfill(session, order.id)
    assert order.status == OrderStatus.COMPLETED
    assert item.status == stock.SOLD
    assert order.params["stock_item_id"] == item.id
    assert len(await stock.for_customer(session, customer.id)) == 1
    assert await stock.take(session, order) is item


async def test_national_switch_invalidates_unpaid_quote_and_refunds_release_reservation(session):
    shop, panel, plan, target, customer = await national_env(session)
    order = await orders.create(
        session, customer, await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan)
    )
    panel.national_mode = False
    with pytest.raises(orders.OrderError, match="plan_unavailable"):
        await orders.settle_from_wallet(session, order.id)
    panel.national_mode = True
    await orders.settle_from_wallet(session, order.id)
    await orders.refund_failed(session, order, "test")
    await session.flush()
    assert (await stock.counts(session, [target.id]))[target.id][stock.AVAILABLE] == 1
    assert not await stock.for_customer(session, customer.id)


async def test_unmapped_national_plan_hidden_and_not_remote_selected(session):
    shop, panel, plan, target, customer = await national_env(session)
    plan.national_stock_plan_id = None
    await session.flush()
    assert await catalog.get_plan(session, shop.id, plan.id, customer) is None
    assert await catalog.pick_panel(session, plan) is None
    with pytest.raises(stock.StockError, match="plan_unavailable"):
        await national.route(session, plan)


async def test_admin_scoping_and_sold_history_protection(session):
    shop, panel, plan, target, customer = await national_env(session)
    with pytest.raises(HTTPException) as denied:
        manual_sales.shop_scope(SimpleNamespace(scoped=True))
    assert denied.value.status_code == 403
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        plan_id=target.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        params={},
    )
    session.add(order)
    await session.flush()
    item = await stock.take(session, order)
    ctx = SimpleNamespace(shop_id=shop.id, scoped=False)
    with pytest.raises(HTTPException) as denied:
        await manual_sales.bulk_inventory(manual_sales.Bulk(ids=[item.id], action="delete"), ctx, session)
    assert denied.value.status_code == 409
    stranger = await factories.customer(session, shop)
    with pytest.raises(HTTPException) as denied:
        await webapp_stock.owned(session, SimpleNamespace(customer=stranger), item.id)
    assert denied.value.status_code == 404
    assert await webapp_stock.owned(session, SimpleNamespace(customer=customer), item.id) is item


async def test_concurrent_same_order_and_different_orders_never_share(engine):
    from uuid import uuid4

    schema = f"stock_test_{uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    async with isolated.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(isolated, expire_on_commit=False)
    async with maker() as db:
        from paurubot.db.models import Customer

        shop = Shop(title="Concurrent stock test")
        db.add(shop)
        await db.flush()
        customer = Customer(shop_id=shop.id, telegram_id=-999999999991)
        db.add(customer)
        await db.flush()
        plan = Plan(shop_id=shop.id, title="Concurrent", delivery="stock")
        db.add(plan)
        await db.flush()
        await stock.add(db, plan, "one\ntwo")
        rows = [
            Order(
                shop_id=shop.id,
                customer_id=customer.id,
                plan_id=plan.id,
                kind=OrderKind.PURCHASE,
                status=OrderStatus.PAID,
                params={},
            )
            for _ in range(3)
        ]
        db.add_all(rows)
        await db.commit()
        ids = [o.id for o in rows]

    async def take(order_id):
        async with maker() as db:
            order = await db.get(Order, order_id)
            item = await stock.take(db, order)
            await db.commit()
            return item.id if item else None

    try:
        same = await asyncio.gather(take(ids[0]), take(ids[0]))
        assert same[0] is not None and same[0] == same[1]
        different = await asyncio.gather(take(ids[1]), take(ids[2]))
        assert sum(i is not None for i in different) == 1
        assert same[0] not in different
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def test_national_renewal_keeps_original_service_and_delivers_replacement(session, monkeypatch):
    from datetime import timedelta

    from paurubot.core import renewal_preview
    from paurubot.db.base import utcnow
    from paurubot.db.models import Service, ServiceStatus
    from paurubot.provisioning.subscription import customer_link

    shop, panel, plan, target, customer = await national_env(session)
    expires = utcnow() + timedelta(days=10)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="original",
        status=ServiceStatus.ACTIVE,
        expire_at=expires,
        subscription_url="https://old.example/sub",
        data_limit=1234567,
    )
    session.add(service)
    await session.flush()
    order = await orders.create(
        session, customer, await orders.draft(session, customer, OrderKind.RENEW, plan=plan, service=service)
    )
    with pytest.raises(orders.OrderError, match="renewal_preview_unavailable"):
        await renewal_preview.for_order(session, customer, order)
    await orders.settle_from_wallet(session, order.id)
    assert await orders.queued_until(session, order) is None

    async def no_op(*args, **kwargs):
        pass

    monkeypatch.setattr(lifecycle, "after_sale", no_op)
    await lifecycle.fulfill(session, order.id)
    assert service.expire_at == expires and service.data_limit == 1234567
    assert (await national.replacement(session, service)).order_id == order.id
    assert customer_link(service, {}) is None
    panel.national_mode = False
    assert await national.route(session, plan, service) is None
    assert customer_link(service, {"branded_sub_enabled": False}) == "https://old.example/sub"


async def test_same_file_renamed_or_pasted_is_not_reimported(session):
    shop = await factories.shop(session)
    plan = Plan(shop_id=shop.id, title="Files", delivery="stock")
    session.add(plan)
    await session.flush()
    for name, expected in [("one.conf", 1), ("renamed.conf", 0)]:
        result = await manual_stock.add(
            session,
            plan,
            manual_stock.Import(
                plan_id=plan.id,
                name="x",
                parts=[
                    manual_stock.Part(filename=name, data=base64.b64encode(b"vless://identical").decode())
                ],
            ),
        )
        assert result["added"] == expected
    assert (await stock.add(session, plan, "vless://identical")).duplicates == 1


async def test_private_binary_download_and_telegram_bundle(session, monkeypatch):
    from paurubot.notifications import manual_delivery, telegram

    shop, panel, plan, target, customer = await national_env(session)
    blob = b"\x00\xff\x01config"
    await manual_stock.add(
        session,
        target,
        manual_stock.Import(
            plan_id=target.id,
            name="Bundle",
            mode="bundle",
            parts=[
                manual_stock.Part(text="vless://bundle", as_file=True, filename="one.txt"),
                manual_stock.Part(
                    filename="two.conf",
                    data=base64.b64encode(blob).decode(),
                    subscription_url="https://example.com/sub",
                ),
            ],
        ),
    )
    # Sell the second (bundle) unit explicitly, preserving its buyer/order bindings.
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        plan_id=target.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        params={},
    )
    session.add(order)
    await session.flush()
    item = await session.scalar(
        select(StockItem).where(StockItem.plan_id == target.id).order_by(StockItem.id.desc()).limit(1)
    )
    item.status = stock.SOLD
    item.order_id = order.id
    item.customer_id = customer.id
    response = await webapp_stock.download(item.id, 1, SimpleNamespace(customer=customer), session)
    assert response.body == blob and response.headers["cache-control"] == "no-store"
    sent = []

    async def document(chat_id, file, **kwargs):
        sent.append((file.filename, file.data))

    async def message(*args, **kwargs):
        pass

    monkeypatch.setattr(telegram, "send_document", document)
    monkeypatch.setattr(telegram, "send_text", message)
    await manual_delivery.send(customer, item, "Ready")
    assert sent == [("one.txt", b"vless://bundle"), ("two.conf", blob)]


async def test_http_inventory_permissions_filters_and_audited_reveal(session, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from paurubot.api.deps import current_staff, get_db
    from paurubot.audit.log import Actor
    from paurubot.db.models import AuditLog

    shop, panel, plan, target, customer = await national_env(session)
    permissions = {"catalog.read", "catalog.write"}
    ctx = SimpleNamespace(shop_id=shop.id, scoped=False, actor=Actor.system(), can=lambda p: p in permissions)
    app = FastAPI()
    app.include_router(manual_sales.router)

    async def staff():
        return ctx

    async def database():
        yield session

    app.dependency_overrides[current_staff] = staff
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.example"
    ) as client:
        response = await client.get(
            "/api/admin/manual-sales",
            params={"search": "Emergency", "plan_id": target.id, "status": "available"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1 and "payload" not in body["items"][0]
        item_id = body["items"][0]["id"]
        response = await client.get(f"/api/admin/manual-sales/{item_id}/content")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["parts"][0]["text"] == "vless://replacement"
        assert await session.scalar(
            select(AuditLog.id).where(AuditLog.action == "stock.view", AuditLog.entity_id == item_id)
        )
        response = await client.put(f"/api/admin/manual-sales/national/{panel.id}", json={"enabled": False})
        assert response.status_code == 403
        response = await client.post(
            "/api/admin/manual-sales/import",
            json={"plan_id": target.id, "name": "HTTP import", "parts": [{"text": "vless://http-import"}]},
        )
        assert response.status_code == 200 and response.json()["added"] == 1
        monkeypatch.setattr(manual_sales, "MAX_IMPORT_REQUEST", 32)
        response = await client.post("/api/admin/manual-sales/import", content=b"x" * 100)
        assert response.status_code == 413
        permissions.remove("catalog.write")
        assert (await client.get(f"/api/admin/manual-sales/{item_id}/content")).status_code == 403
        ctx.scoped = True
        assert (await client.get("/api/admin/manual-sales")).status_code == 403
