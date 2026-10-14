"""Both receipt URLs must use the same guarded pipeline and the payment's shop bot."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from paurubot.api import webapp_auth
from paurubot.api.routes import webapp, webapp_receipts
from paurubot.db.models import PaymentStatus
from paurubot.notifications import handlers, telegram


def png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 30, 80)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def compat_api(monkeypatch):  # type: ignore[no-untyped-def]
    payment = SimpleNamespace(
        id=19,
        shop_id=9,
        customer_id=7,
        provider="card",
        status=PaymentStatus.PENDING,
        amount=100,
        currency="IRT",
        order_id=3,
        fulfilled_at=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        meta={},
        receipt_file_id=None,
        receipt_text=None,
    )
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    ctx = SimpleNamespace(
        session=session, shop=SimpleNamespace(id=9), customer=SimpleNamespace(id=7, telegram_id=777)
    )
    owned = AsyncMock(return_value=payment)
    store, enqueue = AsyncMock(return_value="shop-nine-photo"), AsyncMock()

    async def attach(db, item, **kwargs):  # type: ignore[no-untyped-def]
        item.receipt_file_id = kwargs["file_id"]
        item.status = PaymentStatus.VERIFYING
        item.meta = {**item.meta, "receipt_at": datetime.now(UTC).isoformat()}
        return None

    attach_mock = AsyncMock(side_effect=attach)
    monkeypatch.setattr(webapp_receipts, "_owned", owned)
    monkeypatch.setattr(webapp_receipts, "_store", store)
    monkeypatch.setattr(webapp_receipts.engine, "attach_receipt", attach_mock)
    monkeypatch.setattr(webapp_receipts.queue, "enqueue", enqueue)
    app = FastAPI()
    app.include_router(webapp_receipts.router, prefix="/api/webapp")
    app.add_api_route("/api/webapp/payments/{payment_id}/receipt", webapp.upload_receipt, methods=["POST"])
    app.dependency_overrides[webapp_auth.webapp_context] = lambda: ctx
    return app, payment, ctx, owned, store, attach_mock, enqueue


async def legacy(fixture, *, token=None, data=None):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fixture[0]), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/webapp/payments/19/receipt",
            data={} if token is None else {"expected_revision": token},
            files={"file": ("receipt.png", png() if data is None else data, "image/png")},
        )


def correction(payment) -> None:  # type: ignore[no-untyped-def]
    payment.meta = {
        "fix_requests": [{"kind": "card", "note": "correct it", "at": datetime.now(UTC).isoformat()}]
    }


async def test_initial_legacy_upload_is_locked_and_committed(compat_api):  # type: ignore[no-untyped-def]
    result = await legacy(compat_api)
    assert result.status_code == 200 and result.json() == {"status": "verifying"}
    compat_api[3].assert_awaited_once_with(compat_api[2], 19, lock=True)
    compat_api[2].session.commit.assert_awaited_once()
    assert compat_api[6].await_args.kwargs["dedupe_key"].startswith("payment:19:review:")
    assert (await legacy(compat_api)).status_code == 409
    assert compat_api[4].await_count == 1


async def test_correction_without_revision_cannot_use_legacy_url(compat_api):  # type: ignore[no-untyped-def]
    correction(compat_api[1])
    result = await legacy(compat_api)
    assert result.status_code == 409 and result.json()["detail"] == "receipt_revision_required"
    compat_api[4].assert_not_awaited()


async def test_correction_with_matching_revision_can_use_legacy_url(compat_api):  # type: ignore[no-untyped-def]
    correction(compat_api[1])
    token = webapp_receipts.revision(compat_api[1])
    assert (await legacy(compat_api, token=token)).status_code == 200


async def test_stale_legacy_revision_rejected_before_storage(compat_api):  # type: ignore[no-untyped-def]
    assert (await legacy(compat_api, token="0" * 64)).status_code == 409
    compat_api[4].assert_not_awaited()


async def test_malformed_legacy_revision_rejected_by_schema(compat_api):  # type: ignore[no-untyped-def]
    assert (await legacy(compat_api, token="bad")).status_code == 422
    compat_api[3].assert_not_awaited()


async def test_expired_legacy_payment_rejected_before_storage(compat_api):  # type: ignore[no-untyped-def]
    compat_api[1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await legacy(compat_api)).status_code == 409
    compat_api[4].assert_not_awaited()


@pytest.mark.parametrize("data,code", [(b"not an image", 400), (b"x" * (8 * 1024 * 1024 + 1), 413)])
async def test_legacy_checks_image_bytes_and_size(compat_api, data, code):  # type: ignore[no-untyped-def]
    assert (await legacy(compat_api, data=data)).status_code == code
    compat_api[4].assert_not_awaited()


async def test_legacy_storage_failure_never_attaches_or_queues(compat_api):  # type: ignore[no-untyped-def]
    compat_api[4].return_value = None
    assert (await legacy(compat_api)).status_code == 502
    compat_api[5].assert_not_awaited()
    compat_api[6].assert_not_awaited()
    compat_api[2].session.commit.assert_not_awaited()


@pytest.mark.parametrize("status", [PaymentStatus.PAID, PaymentStatus.FAILED])
async def test_legacy_returns_actual_engine_result(compat_api, status):  # type: ignore[no-untyped-def]
    async def attach(db, item, **kwargs):  # type: ignore[no-untyped-def]
        item.receipt_file_id = kwargs["file_id"]
        item.status = status

    compat_api[5].side_effect = attach
    result = await legacy(compat_api)
    assert result.status_code == 200 and result.json() == {"status": status.value}
    compat_api[6].assert_not_awaited()
    compat_api[2].session.commit.assert_awaited_once()


async def test_legacy_expiry_during_storage_does_not_attach(compat_api):  # type: ignore[no-untyped-def]
    async def store(ctx, data):  # type: ignore[no-untyped-def]
        compat_api[1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return "photo"

    compat_api[4].side_effect = store
    assert (await legacy(compat_api)).status_code == 409
    compat_api[5].assert_not_awaited()
    compat_api[6].assert_not_awaited()


async def test_legacy_commit_failure_never_reports_success(compat_api):  # type: ignore[no-untyped-def]
    compat_api[2].session.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        await legacy(compat_api)


async def test_receipt_storage_and_staff_delivery_use_same_shop(monkeypatch):  # type: ignore[no-untyped-def]
    message = SimpleNamespace(photo=[SimpleNamespace(file_id="shop-nine-photo")])
    send_photo = AsyncMock(return_value=message)
    monkeypatch.setattr(telegram, "send_photo", send_photo)
    monkeypatch.setattr(handlers, "staff_chats", AsyncMock(return_value=[888]))
    ctx = SimpleNamespace(shop=SimpleNamespace(id=9), customer=SimpleNamespace(id=7, telegram_id=777))
    file_id = await webapp._store_receipt(ctx, png())
    assert file_id == "shop-nine-photo"
    assert send_photo.await_args.args[0] == 777
    assert send_photo.await_args.kwargs["shop_id"] == 9
    await handlers._to_staff(SimpleNamespace(), 9, "payments.review", "receipt", photo=file_id)
    assert send_photo.await_args.args[:2] == (888, "shop-nine-photo")
    assert send_photo.await_args.kwargs["shop_id"] == 9


@pytest.mark.parametrize("mode", ["stopped", "empty", "error"])
async def test_storage_failure_has_no_main_bot_fallback(monkeypatch, mode):  # type: ignore[no-untyped-def]
    send_photo = AsyncMock(return_value=None if mode == "stopped" else SimpleNamespace(photo=[]))
    if mode == "error":
        send_photo.side_effect = RuntimeError("child bot unavailable")
    main_bot = AsyncMock()
    monkeypatch.setattr(telegram, "send_photo", send_photo)
    monkeypatch.setattr(telegram, "get_bot", main_bot)
    ctx = SimpleNamespace(shop=SimpleNamespace(id=9), customer=SimpleNamespace(id=7, telegram_id=777))
    assert await webapp._store_receipt(ctx, png()) is None
    assert send_photo.await_args.kwargs["shop_id"] == 9
    main_bot.assert_not_called()
