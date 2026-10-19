"""Unsequenced webhook payloads are hints, never authoritative service state."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from paurubot.db.models import Job, Service, ServiceStatus, ServiceUsage
from paurubot.panels import PanelUser
from paurubot.provisioning import sync
from tests import factories

pytestmark = pytest.mark.security


async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    panel = await factories.panel(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="hint-user",
        status=ServiceStatus.DISABLED,
        used_bytes=200,
        data_limit=1000,
    )
    session.add(service)
    await session.flush()
    return panel, service


async def test_stale_active_payload_cannot_reactivate_or_reduce_usage(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    panel, service = await env(session)
    alerts = AsyncMock()
    monkeypatch.setattr(sync, "evaluate_alerts", alerts)
    result = await sync.apply_webhook_user(
        session, panel, PanelUser("hint-user", status="active", used_bytes=100)
    )
    assert result is service
    assert service.status == ServiceStatus.DISABLED and service.used_bytes == 200
    assert service.synced_at is None
    assert await session.scalar(select(func.count()).select_from(ServiceUsage)) == 0
    alerts.assert_not_awaited()


async def test_webhook_enqueues_only_resource_identity_not_payload_state(session) -> None:  # type: ignore[no-untyped-def]
    panel, service = await env(session)
    await sync.apply_webhook_user(session, panel, PanelUser("hint-user", status="expired", used_bytes=999))
    job = await session.scalar(select(Job).where(Job.kind == "refresh_service"))
    assert job is not None
    assert job.shop_id == service.shop_id and job.queue == "provision"
    assert job.payload == {"service_id": service.id, "panel_id": panel.id}


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_and_deleted_services_do_not_queue(session, deleted) -> None:  # type: ignore[no-untyped-def]
    panel, service = await env(session)
    if deleted:
        service.status = ServiceStatus.DELETED
    result = await sync.apply_webhook_user(session, panel, PanelUser("hint-user" if deleted else "stranger"))
    assert result is None
    assert await session.scalar(select(func.count(Job.id)).where(Job.kind == "refresh_service")) == 0


def claimed(service, panel, **kw):  # type: ignore[no-untyped-def]
    from paurubot.worker.queue import Claimed

    return Claimed(
        1,
        "refresh_service",
        {"service_id": service.id, "panel_id": panel.id},
        1,
        5,
        kw.get("shop_id", service.shop_id),
        "test-token",
    )


@pytest.mark.parametrize("used", [0, 300])
async def test_worker_reads_current_provider_not_old_event(session, monkeypatch, used) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from paurubot.provisioning import lifecycle
    from paurubot.worker.jobs import refresh_panel_hint

    panel, service = await env(session)
    await sync.apply_webhook_user(session, panel, PanelUser("hint-user", status="active", used_bytes=999))
    driver = SimpleNamespace(
        get_user=AsyncMock(
            return_value=PanelUser("hint-user", status="disabled", used_bytes=used, usage_known=True)
        )
    )
    monkeypatch.setattr(lifecycle, "driver_for", AsyncMock(return_value=driver))
    alerts = AsyncMock()
    monkeypatch.setattr(sync, "evaluate_alerts", alerts)
    await refresh_panel_hint(session, claimed(service, panel))
    assert service.status == ServiceStatus.DISABLED and service.used_bytes == used
    driver.get_user.assert_awaited_once()
    alerts.assert_awaited_once_with(session, service, ServiceStatus.DISABLED)
    assert await session.scalar(select(func.count()).select_from(ServiceUsage)) == 1


async def test_provider_failure_does_not_mutate_service(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from paurubot.panels import PanelUnavailable
    from paurubot.provisioning import lifecycle
    from paurubot.worker.jobs import refresh_panel_hint

    panel, service = await env(session)
    monkeypatch.setattr(
        lifecycle,
        "driver_for",
        AsyncMock(return_value=SimpleNamespace(get_user=AsyncMock(side_effect=PanelUnavailable("offline")))),
    )
    with pytest.raises(PanelUnavailable):
        await refresh_panel_hint(session, claimed(service, panel))
    assert service.status == ServiceStatus.DISABLED and service.used_bytes == 200
    assert service.synced_at is None


@pytest.mark.parametrize("bad_shop", [None, 999999])
async def test_worker_rejects_missing_or_wrong_tenant(session, monkeypatch, bad_shop) -> None:  # type: ignore[no-untyped-def]
    from paurubot.provisioning import lifecycle
    from paurubot.worker.jobs import refresh_panel_hint
    from paurubot.worker.runner import Permanent

    panel, service = await env(session)
    read = AsyncMock()
    monkeypatch.setattr(lifecycle, "refresh", read)
    with pytest.raises(Permanent):
        await refresh_panel_hint(session, claimed(service, panel, shop_id=bad_shop))
    read.assert_not_awaited()


@pytest.mark.parametrize("condition", ["deleted", "inactive", "gone", "moved"])
async def test_delayed_job_handles_resource_changes(session, monkeypatch, condition) -> None:  # type: ignore[no-untyped-def]
    from paurubot.provisioning import lifecycle
    from paurubot.worker.jobs import refresh_panel_hint
    from paurubot.worker.runner import Permanent

    panel, service = await env(session)
    job = claimed(service, panel)
    if condition == "deleted":
        service.status = ServiceStatus.DELETED
    elif condition == "inactive":
        panel.is_active = False
    elif condition == "gone":
        job.payload["service_id"] = 999999
    else:
        job.payload["panel_id"] = panel.id + 1
    await session.flush()
    read = AsyncMock()
    monkeypatch.setattr(lifecycle, "refresh", read)
    if condition == "moved":
        with pytest.raises(Permanent):
            await refresh_panel_hint(session, job)
    else:
        await refresh_panel_hint(session, job)
    read.assert_not_awaited()


async def test_later_webhooks_are_not_permanently_deduplicated(session) -> None:  # type: ignore[no-untyped-def]
    panel, service = await env(session)
    for _ in range(2):
        await sync.apply_webhook_user(session, panel, PanelUser("hint-user"))
    assert await session.scalar(select(func.count(Job.id)).where(Job.kind == "refresh_service")) == 2
