"""Unwired owner-scoped mutations for ALREADY BOUND custom Tonpay invoices.

No create/bind/settlement route. Explicit policy and quote originate in a future
trusted creation workflow, not request bodies. Private commits precede remote I/O;
uncertain mutations block all replacement keys until audited reconciliation.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentPurpose, PaymentStatus, Shop, ShopStatus
from paurubot.db.session import get_sessionmaker
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.tonpay_custom import CustomInvoice, TonpayCustomClient, receipt_image

STATE_KEY = "tonpay_custom_v1"
MAX_OPERATIONS = 16  # bounded evidence; never evict old dedupe keys to allow another mutation
Operation = Literal["receipt", "change_card"]


class CustomStateError(PaymentError):
    def __init__(self, code: str) -> None:
        super().__init__("gateway_error")
        self.code = code


@dataclass(frozen=True, slots=True)
class Owner:
    """Derived by a trusted adapter from its authenticated principal, NOT body fields."""

    shop_id: int
    customer_id: int


@dataclass(frozen=True, slots=True)
class MutationResult:
    payment_id: int
    operation: str
    quote_revision: int
    payable_amount: int
    receipt_received: bool
    needs_review: bool


def _integer(value: Any, *, minimum: int = 1, maximum: int = 10**18 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CustomStateError("invalid_state")
    return value


def _now() -> int:
    return int(utcnow().timestamp())


async def _owned(session: AsyncSession, owner: Owner, payment_id: int) -> Payment:
    _integer(owner.shop_id)
    _integer(owner.customer_id)
    _integer(payment_id)
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == owner.customer_id, Customer.shop_id == owner.shop_id, Customer.is_banned.is_(False)
        )
    )
    shop = await session.scalar(
        select(Shop).where(Shop.id == owner.shop_id, Shop.status == ShopStatus.ACTIVE)
    )
    if customer is None or shop is None or not 0 < customer.telegram_id < 2**63:
        raise CustomStateError("not_found")
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.id == payment_id,
            Payment.customer_id == owner.customer_id,
            Payment.shop_id == owner.shop_id,
            Payment.provider == "tonpay_custom",
            Payment.purpose == PaymentPurpose.TOPUP,
        )
        .with_for_update()
    )
    if payment is None:
        raise CustomStateError("not_found")
    if payment.currency != "IRT":
        raise CustomStateError("invalid_state")
    return payment


def _state(payment: Payment) -> dict[str, Any]:
    raw = (payment.meta or {}).get(STATE_KEY)
    if not isinstance(raw, dict):
        raise CustomStateError("invalid_state")
    state = deepcopy(raw)
    quote = state.get("quote")
    policy = state.get("policy")
    operations = state.get("operations")
    if (
        state.get("schema") != 1
        or isinstance(state.get("schema"), bool)
        or not isinstance(quote, dict)
        or not isinstance(policy, dict)
        or not isinstance(operations, dict)
        or len(operations) > MAX_OPERATIONS
        or not isinstance(payment.provider_ref, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payment.provider_ref)
        or state.get("invoice_id") != payment.provider_ref
        or quote.get("request_amount") != payment.amount
        or isinstance(quote.get("request_amount"), bool)
        or not isinstance(state.get("remote_status"), str)
        or len(state["remote_status"]) > 40
        or not isinstance(state.get("paid"), bool)
        or not isinstance(state.get("receipt_received"), bool)
        or not isinstance(state.get("change_card_exhausted"), bool)
    ):
        raise CustomStateError("invalid_state")
    _integer(state.get("schema"), maximum=1)
    _integer(payment.amount)
    _integer(quote.get("request_amount"))
    _integer(quote.get("revision"))
    _integer(quote.get("payable_amount"), minimum=payment.amount)
    _integer(state.get("created_at"))
    for key in ("receipt_wait", "card_cooldown", "review_window"):
        # No global/default product timing is adopted. Creation must freeze an explicit policy.
        _integer(policy.get(key), maximum=30 * 86400)
    for key in ("last_change_at", "review_until", "consent_revision", "receipt_quote_revision"):
        if state.get(key) is not None:
            _integer(state[key])
    for key, op in operations.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or not isinstance(op, dict)
            or op.get("state") not in ("uncertain", "done")
            or not isinstance(op.get("fingerprint"), dict)
            or not isinstance(op.get("token"), str)
        ):
            raise CustomStateError("invalid_state")
    return state


def _save(payment: Payment, state: dict[str, Any]) -> None:
    payment.meta = {**(payment.meta or {}), STATE_KEY: state}


def _active(payment: Payment) -> None:
    if (
        payment.status != PaymentStatus.PENDING
        or payment.expires_at is None
        or payment.expires_at <= utcnow()
    ):
        raise CustomStateError("payment_closed")


def _no_uncertain(state: dict[str, Any]) -> None:
    if any(op["state"] == "uncertain" for op in state["operations"].values()):
        raise CustomStateError("mutation_uncertain")


async def consent(owner: Owner, payment_id: int, quote_revision: int, payable_amount: int) -> None:
    """Persist acknowledgement of the EXACT current payable quote, never a price override."""
    _integer(quote_revision)
    _integer(payable_amount)
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        state = _state(payment)
        _active(payment)
        _no_uncertain(state)
        if state.get("review_until") is not None and _now() >= state["review_until"]:
            raise CustomStateError("review_expired")
        if state["paid"] or state["remote_status"] not in {"pending", "need_action"}:
            raise CustomStateError("action_unavailable")
        if (quote_revision, payable_amount) != (state["quote"]["revision"], state["quote"]["payable_amount"]):
            raise CustomStateError("stale_quote")
        state["consent_revision"] = quote_revision
        _save(payment, state)
        await session.commit()


async def _claim(
    owner: Owner, payment_id: int, key: str, operation: Operation, revision: int, digest: str
) -> tuple[str, str, str, int, MutationResult | None]:
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise CustomStateError("invalid_key")
    _integer(revision)
    storage_key = hashlib.sha256(
        f"{owner.shop_id}:{owner.customer_id}:{payment_id}:{key}".encode()
    ).hexdigest()
    fingerprint = {"operation": operation, "quote_revision": revision, "sha256": digest}
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        state = _state(payment)
        previous = state["operations"].get(storage_key)
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise CustomStateError("intent_conflict")
            if previous["state"] != "done":
                raise CustomStateError("mutation_uncertain")
            try:
                result = MutationResult(**previous["result"])
            except (KeyError, TypeError):
                raise CustomStateError("invalid_state") from None
            _integer(result.quote_revision)
            _integer(result.payable_amount, minimum=payment.amount)
            if (
                result.payment_id != payment.id
                or isinstance(result.payment_id, bool)
                or result.operation != operation
                or not isinstance(result.receipt_received, bool)
                or not isinstance(result.needs_review, bool)
            ):
                raise CustomStateError("invalid_state")
            return storage_key, previous["token"], state["invoice_id"], payment.amount, result
        _active(payment)
        _no_uncertain(state)
        if len(state["operations"]) >= MAX_OPERATIONS:
            raise CustomStateError("history_full")
        if revision != state["quote"]["revision"]:
            raise CustomStateError("stale_quote")
        if state["paid"] or state["remote_status"] not in {"pending", "need_action"}:
            raise CustomStateError("action_unavailable")
        now = _now()
        if state.get("review_until") is not None and now >= state["review_until"]:
            raise CustomStateError("review_expired")
        if operation == "receipt":
            # Provider docs allow an original receipt only while pending. Amount-
            # mismatch/remainder flows use a separate contract, never this endpoint.
            if state["remote_status"] != "pending":
                raise CustomStateError("action_unavailable")
            if state.get("consent_revision") != revision:
                raise CustomStateError("consent_required")
            if now < state["created_at"] + state["policy"]["receipt_wait"]:
                raise CustomStateError("receipt_not_ready")
            if (
                state["receipt_received"]
                and state["remote_status"] != "need_action"
                and state.get("receipt_quote_revision", revision) == revision
            ):
                raise CustomStateError("action_unavailable")
        elif state["change_card_exhausted"] or (
            state.get("last_change_at") is not None
            and now < state["last_change_at"] + state["policy"]["card_cooldown"]
        ):
            raise CustomStateError("card_change_unavailable")
        token = uuid.uuid4().hex
        state["operations"][storage_key] = {
            "state": "uncertain",
            "token": token,
            "fingerprint": fingerprint,
            "started_at": now,
            "observed_status": state["remote_status"],
            "observed_paid": state["paid"],
        }
        _save(payment, state)
        # No remote call has occurred. This independent commit survives caller rollback.
        await session.commit()
        return storage_key, token, state["invoice_id"], payment.amount, None


async def _finish(
    owner: Owner, payment_id: int, storage_key: str, token: str, operation: Operation, invoice: CustomInvoice
) -> MutationResult:
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        state = _state(payment)
        record = state["operations"].get(storage_key)
        if record is None or record["token"] != token or record["state"] != "uncertain":
            raise CustomStateError("mutation_uncertain")
        if (
            invoice.invoice_id != payment.provider_ref
            or invoice.order_id not in (None, str(payment.id))
            or invoice.request_amount != payment.amount
            or invoice.payable_amount < payment.amount
            or (operation == "receipt" and not invoice.receipt_received)
        ):
            raise CustomStateError("invalid_response")
        if state["quote"]["revision"] != record["fingerprint"]["quote_revision"]:
            raise CustomStateError("mutation_uncertain")
        changed = invoice.payable_amount != state["quote"]["payable_amount"]
        if changed or operation == "change_card":
            state["quote"] = {
                **state["quote"],
                "revision": state["quote"]["revision"] + 1,
                "payable_amount": invoice.payable_amount,
            }
            state["consent_revision"] = None  # new card OR fee needs fresh acknowledgement
        advanced = (
            state["remote_status"] != record["observed_status"] or state["paid"] != record["observed_paid"]
        )
        if not advanced:
            state["remote_status"] = invoice.status
        # A concurrent authenticated observation must never be downgraded by an older reply.
        state["paid"] = state["paid"] or invoice.paid
        state["change_card_exhausted"] = state["change_card_exhausted"] or invoice.change_card_exhausted
        if operation == "change_card":
            state["last_change_at"] = _now()
        else:
            state["receipt_received"] = True
            state["receipt_quote_revision"] = state["quote"]["revision"]
            state["review_until"] = state.get("review_until") or (_now() + state["policy"]["review_window"])
        result = MutationResult(
            payment.id,
            operation,
            state["quote"]["revision"],
            invoice.payable_amount,
            operation == "receipt",
            changed
            or advanced
            or state["paid"]
            or payment.status != PaymentStatus.PENDING
            or payment.expires_at is None
            or payment.expires_at <= utcnow()
            or (state.get("review_until") is not None and state["review_until"] <= _now()),
        )
        record.update(state="done", finished_at=_now(), result=asdict(result))
        _save(payment, state)
        # Preserve concurrent administrative status changes and unrelated metadata.
        # No Payment.status/provider_ref/balance/journal/fulfilment mutation here.
        await session.commit()
        return result


async def change_card(
    owner: Owner, payment_id: int, key: str, quote_revision: int, *, client: TonpayCustomClient
) -> MutationResult:
    storage, token, reference, amount, replay = await _claim(
        owner, payment_id, key, "change_card", quote_revision, ""
    )
    if replay is not None:
        return replay
    invoice = await client.change_card(reference, str(payment_id), amount)
    return await _finish(owner, payment_id, storage, token, "change_card", invoice)


async def submit_receipt(
    owner: Owner,
    payment_id: int,
    key: str,
    quote_revision: int,
    content: bytes,
    *,
    client: TonpayCustomClient,
) -> MutationResult:
    # Deny cross-owner/tenant/guest/banned requests before expensive image processing.
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        _state(payment)
    image = await asyncio.to_thread(receipt_image, content)
    digest = hashlib.sha256(image.content).hexdigest()
    # Recheck ownership and ALL action gates after image processing, under a fresh lock.
    storage, token, reference, amount, replay = await _claim(
        owner, payment_id, key, "receipt", quote_revision, digest
    )
    if replay is not None:
        return replay
    invoice = await client.submit_receipt(reference, str(payment_id), amount, image.content)
    return await _finish(owner, payment_id, storage, token, "receipt", invoice)
