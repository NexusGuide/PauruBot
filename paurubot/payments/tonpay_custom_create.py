"""Unwired, single-attempt creation of custom Tonpay ordinary top-ups.

The server-only configuration is an explicit release gate, not a customer DTO.
No registry, callback route, key configuration, UI, settlement or recovery hook.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentPurpose, PaymentStatus, Shop, ShopStatus
from paurubot.db.session import get_sessionmaker
from paurubot.payments.providers.tonpay_custom import (
    CustomAPIError,
    CustomInvoice,
    TonpayCustomClient,
    _callback,
)
from paurubot.payments.tonpay_custom_service import (
    STATE_KEY,
    CustomStateError,
    Owner,
    _integer,
    _state,
    keep_card,
)

CREATE_KEY = "tonpay_custom_create_v1"


@dataclass(frozen=True, slots=True)
class CreationPolicy:
    """Approved frozen server policy. No Faoxima timings/fees adopted as defaults."""

    minimum_amount: int
    maximum_amount: int
    invoice_ttl: int
    receipt_wait: int
    card_cooldown: int
    review_window: int


@dataclass(frozen=True, slots=True)
class CreationConfig:
    """Trusted adapter only; enabled defaults false. Epoch is NONSECRET key identity.

    Future adapter must load policy/key/callback for this shop, never body fields,
    and construct the matching client. This object is NOT authentication or RBAC.
    """

    shop_id: int
    credential_epoch: str
    callback_url: str
    policy: CreationPolicy
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class CreatedQuote:
    payment_id: int
    quote_revision: int
    request_amount: int
    payable_amount: int
    needs_review: bool


def _request(owner: Owner, amount: int, key: str, cfg: CreationConfig) -> tuple[str, dict[str, Any]]:
    _integer(owner.shop_id)
    _integer(owner.customer_id)
    _integer(amount)
    _integer(cfg.shop_id)
    if cfg.enabled is not True or cfg.shop_id != owner.shop_id or isinstance(cfg.shop_id, bool):
        raise CustomStateError("creation_disabled")
    if not isinstance(cfg.credential_epoch, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{16,64}", cfg.credential_epoch
    ):
        raise CustomStateError("invalid_config")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise CustomStateError("invalid_key")
    policy = asdict(cfg.policy)
    for name in ("minimum_amount", "maximum_amount"):
        _integer(policy[name])
    if not policy["minimum_amount"] <= amount <= policy["maximum_amount"]:
        raise CustomStateError("invalid_amount")
    for name in ("invoice_ttl", "receipt_wait", "card_cooldown", "review_window"):
        _integer(policy[name], maximum=30 * 86400)
    if policy["invoice_ttl"] <= policy["receipt_wait"]:
        raise CustomStateError("invalid_config")
    callback = _callback(cfg.callback_url)  # syntax only; deployment origin policy still required
    storage = (
        "custom:create:v1:"
        + hashlib.sha256(f"{owner.shop_id}:{owner.customer_id}:{key}".encode()).hexdigest()
    )
    return storage, {
        "amount": amount,
        "currency": "IRT",
        "policy": policy,
        "credential_epoch": cfg.credential_epoch,
        "callback_sha256": hashlib.sha256(callback.encode()).hexdigest(),
    }


async def _principal(session: AsyncSession, owner: Owner) -> Customer:
    # A durable customer row is the claim mutex, not Redis or an expiring lease.
    customer = await session.scalar(
        select(Customer)
        .where(Customer.id == owner.customer_id, Customer.shop_id == owner.shop_id)
        .with_for_update()
    )
    shop = await session.scalar(
        select(Shop).where(Shop.id == owner.shop_id, Shop.status == ShopStatus.ACTIVE)
    )
    if (
        customer is None
        or shop is None
        or customer.is_banned
        or customer.card_trust == "blocked"
        or not 0 < customer.telegram_id < 2**63
    ):
        raise CustomStateError("not_found")
    return customer


def _attempt(payment: Payment) -> dict[str, Any]:
    attempt = (payment.meta or {}).get(CREATE_KEY)
    if (
        not isinstance(attempt, dict)
        or attempt.get("state") not in {"uncertain", "ready", "failed"}
        or not isinstance(attempt.get("request"), dict)
        or not isinstance(attempt.get("token"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", attempt["token"])
    ):
        raise CustomStateError("invalid_state")
    return attempt


def _replay(payment: Payment) -> CreatedQuote:
    if (
        payment.status != PaymentStatus.PENDING
        or payment.expires_at is None
        or payment.expires_at <= utcnow()
    ):
        raise CustomStateError("payment_closed")
    state = _state(payment)
    # Read the CURRENT quote, never revive the initial fee/card revision after a mutation.
    return CreatedQuote(
        payment.id,
        state["quote"]["revision"],
        payment.amount,
        state["quote"]["payable_amount"],
        state["paid"]
        or state["receipt_received"]
        or state["remote_status"] not in {"pending", "need_action"}
        or any(op["state"] == "uncertain" for op in state["operations"].values()),
    )


# A creation whose answer never came (timeout, crash, restart) blocks new invoices for a while,
# so a slow reply cannot leave the buyer with two. The buyer never saw its card, so could not
# have paid it; once its answer can no longer be on the way, it stops blocking. The record is
# kept for review, and its own key never posts again.
STALE_SECONDS = 600


async def _blocking(session: AsyncSession, owner: Owner, exclude: int | None = None) -> bool:
    state = Payment.meta[CREATE_KEY]["state"].as_string()
    started = Payment.meta[CREATE_KEY]["started_at"].as_integer()
    cutoff = int(utcnow().timestamp()) - STALE_SECONDS
    stmt = select(Payment.id).where(
        Payment.shop_id == owner.shop_id,
        Payment.customer_id == owner.customer_id,
        Payment.provider == "tonpay_custom",
        or_(state.is_(None), state == "uncertain"),
        or_(started.is_(None), started > cutoff),
    )
    if exclude is not None:
        stmt = stmt.where(Payment.id != exclude)
    return await session.scalar(stmt.limit(1)) is not None


async def _fail(owner: Owner, pid: int, token: str) -> None:
    """TonPays surely made nothing: close the attempt so it blocks nobody."""
    async with get_sessionmaker()() as session:
        payment = await session.scalar(select(Payment).where(Payment.id == pid).with_for_update())
        if payment is None or payment.shop_id != owner.shop_id or payment.customer_id != owner.customer_id:
            return
        attempt = _attempt(payment)
        if attempt["token"] != token or attempt["state"] != "uncertain" or payment.provider_ref is not None:
            return
        payment.meta = {**(payment.meta or {}), CREATE_KEY: {**attempt, "state": "failed"}}
        if payment.status == PaymentStatus.CREATED:
            payment.status = PaymentStatus.FAILED
        await session.commit()


async def _claim(
    owner: Owner, amount: int, storage: str, fingerprint: dict[str, Any]
) -> tuple[int, int, str, CreatedQuote | None]:
    async with get_sessionmaker()() as session:
        customer = await _principal(session, owner)
        payment = await session.scalar(select(Payment).where(Payment.idempotency_key == storage))
        if payment is not None:
            if (
                payment.shop_id != owner.shop_id
                or payment.customer_id != owner.customer_id
                or payment.provider != "tonpay_custom"
                or payment.purpose != PaymentPurpose.TOPUP
                or payment.currency != "IRT"
                or payment.amount != amount
            ):
                raise CustomStateError("intent_conflict")
            attempt = _attempt(payment)
            if attempt["request"] != fingerprint:
                raise CustomStateError("intent_conflict")
            if attempt["state"] == "uncertain":
                raise CustomStateError("creation_uncertain")
            if attempt["state"] == "failed":
                # TonPays surely made nothing for it: the same key may simply try again.
                if await _blocking(session, owner, exclude=payment.id):
                    raise CustomStateError("creation_uncertain")
                token = uuid.uuid4().hex
                now = utcnow()
                payment.status = PaymentStatus.CREATED
                payment.expires_at = now + timedelta(seconds=fingerprint["policy"]["invoice_ttl"])
                payment.meta = {
                    **(payment.meta or {}),
                    CREATE_KEY: {
                        **attempt,
                        "state": "uncertain",
                        "token": token,
                        "started_at": int(now.timestamp()),
                    },
                }
                pid, buyer = payment.id, customer.telegram_id
                await session.commit()
                return pid, buyer, token, None
            return payment.id, customer.telegram_id, attempt["token"], _replay(payment)
        if await _blocking(session, owner):
            raise CustomStateError("creation_uncertain")
        token = uuid.uuid4().hex
        now = utcnow()
        payment = Payment(
            shop_id=owner.shop_id,
            customer_id=owner.customer_id,
            provider="tonpay_custom",
            purpose=PaymentPurpose.TOPUP,
            status=PaymentStatus.CREATED,
            amount=amount,
            currency="IRT",
            idempotency_key=storage,
            expires_at=now + timedelta(seconds=fingerprint["policy"]["invoice_ttl"]),
            meta={
                CREATE_KEY: {
                    "state": "uncertain",
                    "token": token,
                    "request": fingerprint,
                    "started_at": int(now.timestamp()),
                }
            },
        )
        session.add(payment)
        await session.flush()
        pid, buyer = payment.id, customer.telegram_id
        # Independent session: do not commit the caller's principal or unrelated writes.
        await session.commit()
        return pid, buyer, token, None


async def _bind(owner: Owner, pid: int, token: str, invoice: CustomInvoice) -> CreatedQuote:
    async with get_sessionmaker()() as session:
        await _principal(session, owner)
        # Serialize binding per shop so the same provider reference cannot bind two local payments.
        await session.scalar(select(Shop).where(Shop.id == owner.shop_id).with_for_update())
        payment = await session.scalar(
            select(Payment).where(Payment.id == pid).with_for_update(),
            execution_options={"populate_existing": True},
        )
        if payment is None or payment.shop_id != owner.shop_id or payment.customer_id != owner.customer_id:
            raise CustomStateError("not_found")
        attempt = _attempt(payment)
        if attempt["token"] != token or attempt["state"] != "uncertain" or payment.provider_ref is not None:
            raise CustomStateError("creation_uncertain")
        request = attempt["request"]
        if (
            payment.provider != "tonpay_custom"
            or payment.purpose != PaymentPurpose.TOPUP
            or payment.currency != "IRT"
            or payment.amount != request["amount"]
            or invoice.order_id not in (None, str(pid))
            or invoice.request_amount != payment.amount
            or isinstance(invoice.request_amount, bool)
            or not isinstance(invoice.invoice_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice.invoice_id)
        ):
            raise CustomStateError("invalid_response")
        _integer(invoice.request_amount)
        _integer(invoice.payable_amount, minimum=payment.amount)
        duplicate = await session.scalar(
            select(Payment.id)
            .where(
                Payment.shop_id == owner.shop_id,
                Payment.provider == "tonpay_custom",
                Payment.provider_ref == invoice.invoice_id,
                Payment.id != pid,
            )
            .limit(1)
        )
        if duplicate is not None:
            raise CustomStateError("creation_uncertain")
        policy = request["policy"]
        state = {
            "schema": 1,
            "invoice_id": invoice.invoice_id,
            "quote": {
                "revision": 1,
                "request_amount": payment.amount,
                "payable_amount": invoice.payable_amount,
            },
            "policy": {k: policy[k] for k in ("receipt_wait", "card_cooldown", "review_window")},
            "created_at": attempt["started_at"],
            "remote_status": invoice.status,
            "paid": invoice.paid,
            "receipt_received": invoice.receipt_received,
            "change_card_exhausted": invoice.change_card_exhausted,
            "operations": {},
            "consent_revision": None,
        }
        keep_card(state, invoice)
        if invoice.receipt_received:
            state.update(
                receipt_quote_revision=1, review_until=int(utcnow().timestamp()) + policy["review_window"]
            )
        payment.provider_ref = invoice.invoice_id
        payment.meta = {**(payment.meta or {}), STATE_KEY: state, CREATE_KEY: {**attempt, "state": "ready"}}
        _state(payment)  # validates provider observations before any binding commit
        if payment.status == PaymentStatus.CREATED:
            payment.status = (
                PaymentStatus.EXPIRED
                if payment.expires_at is None or payment.expires_at <= utcnow()
                else PaymentStatus.PENDING
            )
        result = CreatedQuote(
            pid,
            1,
            payment.amount,
            invoice.payable_amount,
            invoice.paid
            or invoice.receipt_received
            or invoice.status not in {"pending", "need_action"}
            or payment.status != PaymentStatus.PENDING
            or payment.expires_at is None
            or payment.expires_at <= utcnow(),
        )
        # Retain references/evidence after concurrent closure; NEVER resurrect or credit the row.
        await session.commit()
        return result


async def create_once(
    owner: Owner, amount: int, key: str, *, config: CreationConfig, client: TonpayCustomClient
) -> CreatedQuote:
    """Internal trusted call only. Same-key replay; uncertain attempts block new keys too."""
    storage, fingerprint = _request(owner, amount, key, config)
    pid, buyer, token, replay = await _claim(owner, amount, storage, fingerprint)
    if replay is not None:
        return replay
    # Single attempt, with no DB row locks held. Any exception leaves the committed marker,
    # except a definite refusal: then nothing exists at TonPays and the attempt is closed.
    try:
        invoice = await client.create(str(pid), amount, buyer, config.callback_url)
    except CustomAPIError as exc:
        if exc.definite:
            await _fail(owner, pid, token)
        raise
    return await _bind(owner, pid, token, invoice)
