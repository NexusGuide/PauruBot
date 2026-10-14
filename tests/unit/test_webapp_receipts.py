"""Receipt API contracts. All Telegram, engine and queue mutations are faked in unit tests."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from PIL import Image

from paurubot.api import webapp_auth
from paurubot.api.routes import webapp_receipts as route
from paurubot.db.models import PaymentStatus


def row(**changes):  # type: ignore[no-untyped-def]
    value = dict(
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
    return SimpleNamespace(**{**value, **changes})


def image(kind="PNG"):  # type: ignore[no-untyped-def]
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 30, 80)).save(buffer, format=kind)
    return buffer.getvalue()


@pytest.fixture
def receipt_api(monkeypatch):  # type: ignore[no-untyped-def]
    payment = row()
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), scalars=AsyncMock(return_value=[]))
    ctx = SimpleNamespace(session=session, shop=SimpleNamespace(id=9), customer=SimpleNamespace(id=7))
    owned, store, notify = AsyncMock(return_value=payment), AsyncMock(return_value="new-photo"), AsyncMock()

    async def attach(db, item, **kwargs):  # type: ignore[no-untyped-def]
        item.receipt_file_id = kwargs["file_id"]
        item.status = PaymentStatus.VERIFYING
        item.meta = {**item.meta, "receipt_at": datetime.now(UTC).isoformat()}
        return None

    attach_mock = AsyncMock(side_effect=attach)
    monkeypatch.setattr(route, "_owned", owned)
    monkeypatch.setattr(route, "_store", store)
    monkeypatch.setattr(route.engine, "attach_receipt", attach_mock)
    monkeypatch.setattr(route.queue, "enqueue", notify)
    app = FastAPI()
    app.include_router(route.router, prefix="/api/webapp")
    app.dependency_overrides[webapp_auth.webapp_context] = lambda: ctx
    return app, payment, ctx, owned, store, attach_mock, notify


async def send(fixture, *, data=None, token=None):  # type: ignore[no-untyped-def]
    app, payment, *_ = fixture
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/webapp/payments/19/receipt-upload",
            data={"expected_revision": token or route.revision(payment)},
            files={"file": ("receipt.png", image() if data is None else data, "image/png")},
        )


async def test_read_only_projection_no_secret_history(receipt_api):  # type: ignore[no-untyped-def]
    app, payment, ctx, owned, store, attach, notify = receipt_api
    payment.meta = {
        "fix_requests": [
            {
                "kind": "amount",
                "note": "<script>literal</script>",
                "at": datetime.now(UTC).isoformat(),
                "by": 666,
                "receipt_file_id": "private",
            }
        ],
        "private": "secret",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.get("/api/webapp/payments/19/receipt-state")).json()
    assert result["phase"] == "correction_required" and result["can_upload"]
    assert result["correction"]["note"] == "<script>literal</script>"
    assert "private" not in str(result) and "666" not in str(result)
    owned.assert_awaited_once_with(ctx, 19)
    store.assert_not_awaited()
    attach.assert_not_awaited()
    notify.assert_not_awaited()
    ctx.session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "status,phase",
    [
        ("verifying", "under_review"),
        ("paid", "paid"),
        ("failed", "closed"),
        ("expired", "closed"),
        ("disputed", "closed"),
        ("refunded", "closed"),
        ("created", "unknown"),
    ],
)
def test_status_projection(status, phase):  # type: ignore[no-untyped-def]
    result = route.receipt_view(row(status=PaymentStatus(status)))
    assert result["phase"] == phase and not result["can_upload"]


@pytest.mark.parametrize(
    "meta",
    [
        {"fix_requests": "bad"},
        {"fix_requests": [None]},
        {"fix_requests": [{"kind": "unknown"}]},
        {"fix_requests": [{"kind": "card", "note": 3, "at": "bad"}]},
        {"fix_requests": [{"kind": "amount", "note": None, "at": "bad"}]},
        {"fix_requests": [{"kind": "amount", "note": None, "at": "2026-01-01T12:00:00"}]},
        [],
    ],
)
def test_malformed_correction_fails_closed(meta):  # type: ignore[no-untyped-def]
    result = route.receipt_view(row(meta=meta))
    assert result["phase"] == "unknown" and not result["can_upload"]


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt_file_id": "existing"},
        {"receipt_text": "existing"},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"provider": "stars"},
        {"status": PaymentStatus.VERIFYING},
    ],
)
async def test_unavailable_upload_has_no_external_effects(receipt_api, changes):  # type: ignore[no-untyped-def]
    _, payment, _, _, store, attach, notify = receipt_api
    for key, value in changes.items():
        setattr(payment, key, value)
    assert (await send(receipt_api)).status_code == 409
    store.assert_not_awaited()
    attach.assert_not_awaited()
    notify.assert_not_awaited()


async def test_stale_revision_rejected_before_storage(receipt_api):  # type: ignore[no-untyped-def]
    assert (await send(receipt_api, token="0" * 64)).status_code == 409
    receipt_api[4].assert_not_awaited()


async def test_missing_or_malformed_revision_rejected_by_schema(receipt_api):  # type: ignore[no-untyped-def]
    assert (await send(receipt_api, token="bad")).status_code == 422
    receipt_api[3].assert_not_awaited()


@pytest.mark.parametrize(
    "data,code",
    [(b"", 400), (b"not an image", 400), (b"<svg></svg>", 400), (b"x" * (8 * 1024 * 1024 + 1), 413)],
)
async def test_invalid_images_never_reach_telegram(receipt_api, data, code):  # type: ignore[no-untyped-def]
    assert (await send(receipt_api, data=data)).status_code == code
    receipt_api[4].assert_not_awaited()
    receipt_api[5].assert_not_awaited()


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "WEBP"])
def test_supported_images(kind):  # type: ignore[no-untyped-def]
    route.validate_image(image(kind))


def test_gif_rejected():
    with pytest.raises(HTTPException):
        route.validate_image(image("GIF"))


async def test_store_failure_never_creates_unviewable_review(receipt_api):  # type: ignore[no-untyped-def]
    receipt_api[4].return_value = None
    assert (await send(receipt_api)).status_code == 502
    receipt_api[5].assert_not_awaited()
    receipt_api[6].assert_not_awaited()
    receipt_api[2].session.commit.assert_not_awaited()


async def test_recorded_upload_locked_then_committed(receipt_api):  # type: ignore[no-untyped-def]
    _, payment, ctx, owned, store, attach, notify = receipt_api
    result = await send(receipt_api)
    assert result.status_code == 200 and result.json()["phase"] == "under_review"
    owned.assert_awaited_once_with(ctx, 19, lock=True)
    store.assert_awaited_once()
    attach.assert_awaited_once()
    ctx.session.refresh.assert_awaited_once_with(payment)
    notify.assert_awaited_once()
    assert notify.await_args.kwargs["dedupe_key"].startswith("payment:19:review:")
    ctx.session.commit.assert_awaited_once()
    assert (await send(receipt_api)).status_code == 409  # A second click cannot submit a second photo.
    assert store.await_count == 1


@pytest.mark.parametrize("status,phase", [(PaymentStatus.PAID, "paid"), (PaymentStatus.FAILED, "closed")])
async def test_actual_engine_outcome_not_always_verifying(receipt_api, status, phase):  # type: ignore[no-untyped-def]
    async def attach(db, payment, **kwargs):  # type: ignore[no-untyped-def]
        payment.receipt_file_id = kwargs["file_id"]
        payment.status = status

    receipt_api[5].side_effect = attach
    result = await send(receipt_api)
    assert result.status_code == 200 and result.json()["phase"] == phase
    receipt_api[6].assert_not_awaited()


async def test_ten_entry_history_does_not_suppress_new_review(receipt_api):  # type: ignore[no-untyped-def]
    _, payment, *_ = receipt_api
    request = {"kind": "card", "note": None, "at": datetime.now(UTC).isoformat()}
    payment.meta = {"fix_requests": [request] * 10}
    keys = []
    for index in range(2):
        payment.status = PaymentStatus.PENDING
        payment.receipt_file_id = None
        receipt_api[4].return_value = f"photo-{index}"
        assert (await send(receipt_api)).status_code == 200
        keys.append(receipt_api[6].await_args.kwargs["dedupe_key"])
    assert keys[0] != keys[1]


async def test_commit_failure_does_not_report_success(receipt_api):  # type: ignore[no-untyped-def]
    receipt_api[2].session.commit.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError, match="commit failed"):
        await send(receipt_api)


async def test_inbox_has_bounded_keyset_and_scoped_query(receipt_api):  # type: ignore[no-untyped-def]
    app, _, ctx, *_ = receipt_api
    ctx.session.scalars.return_value = [row(id=value) for value in range(49, 28, -1)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/webapp/receipts?before=50")
    assert result.status_code == 200
    assert len(result.json()["items"]) == 20 and result.json()["next_before"] == 30
    statement = str(ctx.session.scalars.await_args.args[0])
    assert "shop_id" in statement and "customer_id" in statement and "provider" in statement
    assert "LIMIT" in statement and "payments.id <" in statement


@pytest.mark.security
async def test_owned_payment_enforces_customer_and_shop(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Payment, PaymentPurpose
    from tests import factories

    shop, other = await factories.shop(session), await factories.shop(session)
    customer, stranger = await factories.customer(session, shop), await factories.customer(session, shop)
    payment = Payment(
        shop_id=shop.id,
        customer_id=customer.id,
        purpose=PaymentPurpose.TOPUP,
        provider="card",
        status=PaymentStatus.PENDING,
        amount=100,
        currency="IRT",
        idempotency_key="receipt-ui-security",
    )
    session.add(payment)
    await session.flush()
    ctx = SimpleNamespace(session=session, shop=shop, customer=customer)
    assert (await route._owned(ctx, payment.id, lock=True)).id == payment.id
    for changed in [
        SimpleNamespace(session=session, shop=other, customer=customer),
        SimpleNamespace(session=session, shop=shop, customer=stranger),
    ]:
        with pytest.raises(HTTPException) as exc:
            await route._owned(changed, payment.id)
        assert exc.value.status_code == 404


async def test_deadline_passing_during_storage_does_not_attach_or_enqueue(receipt_api):  # type: ignore[no-untyped-def]
    payment = receipt_api[1]

    async def store(ctx, data):  # type: ignore[no-untyped-def]
        payment.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return "photo-uploaded-before-expiry-check"

    receipt_api[4].side_effect = store
    assert (await send(receipt_api)).status_code == 409
    receipt_api[5].assert_not_awaited()
    receipt_api[6].assert_not_awaited()


async def test_core_corrected_receipt_never_uses_trusted_auto_approval(monkeypatch):  # type: ignore[no-untyped-def]
    # Exercise the existing production attach function, not the route fixture's fake.
    payment = row(meta={"fix_requests": [{"kind": "amount", "at": datetime.now(UTC).isoformat()}]})
    session = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(route.engine.receipts, "sha256", lambda data: "abc")
    monkeypatch.setattr(route.engine.receipts, "dhash", lambda data: "123")
    monkeypatch.setattr(route.engine.receipts, "find_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(
        route.engine.shop_settings, "load", AsyncMock(return_value={"receipt_duplicate_block": True})
    )
    auto, paid = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(route.engine, "auto_approvable", auto)
    monkeypatch.setattr(route.engine, "mark_paid", paid)
    await route.engine.attach_receipt(session, payment, file_id="corrected-photo", text=None, image=image())
    assert payment.status == PaymentStatus.VERIFYING
    auto.assert_not_awaited()
    paid.assert_not_awaited()
