"""Shop-configured custom checkout, authenticated observations and gated confirmation.

An ambiguous POST is never repeated or cleared by a GET observation. In particular,
no lookup-by-order contract exists for a lost creation response: report this honestly.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from paurubot.audit.log import Actor, record
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus
from paurubot.db.session import get_sessionmaker
from paurubot.payments.base import PaymentError
from paurubot.payments.providers.tonpay_custom import TonpayCustomClient, _callback
from paurubot.payments.tonpay_custom_create import CREATE_KEY, CreationConfig, CreationPolicy, create_once
from paurubot.payments.tonpay_custom_service import (
    CustomStateError,
    Owner,
    _integer,
    _now,
    _owned,
    _save,
    _state,
    card_of,
    keep_card,
)

PREFIX = "tonpay_custom_"
POLICY_KEYS = ("receipt_wait", "card_cooldown", "review_window")


def configuration(shop_id: int, cfg: dict[str, Any], low: int, high: int) -> CreationConfig:
    if cfg.get(PREFIX + "enabled") is not True or cfg.get(PREFIX + "policy_approved") is not True:
        raise CustomStateError("method_disabled")
    if not isinstance(cfg.get(PREFIX + "api_key"), str) or not cfg[PREFIX + "api_key"].strip():
        raise CustomStateError("method_disabled")
    ttl = _integer(cfg.get("payment_expire_minutes"), maximum=30 * 24 * 60) * 60
    policy = CreationPolicy(
        low, high, ttl, *(_integer(cfg.get(PREFIX + key), maximum=30 * 86400) for key in POLICY_KEYS)
    )
    callback = _callback(get_settings().public_url + "/api/pay/tonpay-custom")
    result = CreationConfig(shop_id, cfg.get(PREFIX + "credential_epoch", ""), callback, policy, True)
    # Reuse the creator's full policy/epoch validation without creating any records.
    from paurubot.payments.tonpay_custom_create import _request

    _request(Owner(shop_id, 1), low, "configuration-check-only", result)
    return result


async def runtime(owner: Owner) -> tuple[CreationConfig, TonpayCustomClient, bool]:
    from paurubot.payments import engine

    async with get_sessionmaker()() as session:
        customer = await session.scalar(
            select(Customer).where(Customer.id == owner.customer_id, Customer.shop_id == owner.shop_id)
        )
        if customer is None or customer.is_banned or getattr(customer, "card_trust", "") == "blocked":
            raise CustomStateError("not_found")
        cfg = await shop_settings.load(session, owner.shop_id, fresh=True)
        low, high = await engine.topup_bounds(session, customer)
        config = configuration(owner.shop_id, cfg, low, high)
        return (
            config,
            TonpayCustomClient(cfg[PREFIX + "api_key"]),
            cfg.get(PREFIX + "confirm_enabled") is True,
        )


async def available(session: Any, customer: Customer) -> bool:
    from paurubot.payments import engine

    if customer.is_banned or customer.telegram_id <= 0 or getattr(customer, "card_trust", "") == "blocked":
        return False
    try:
        cfg = await shop_settings.load(session, customer.shop_id, fresh=True)
        low, high = await engine.topup_bounds(session, customer)
        configuration(customer.shop_id, cfg, low, high)
        if cfg.get(PREFIX + "confirm_enabled") is not True:
            return False
    except (PaymentError, ValueError):
        return False
    return True


def _uncertain(payment: Payment, state: dict[str, Any] | None) -> bool:
    attempt = (payment.meta or {}).get(CREATE_KEY)
    return (
        not isinstance(attempt, dict)
        or attempt.get("state") != "ready"
        or state is None
        or any(op["state"] == "uncertain" for op in state["operations"].values())
    )


def projection(payment: Payment) -> dict[str, Any]:
    state = _state(payment) if payment.provider_ref else None
    uncertain = _uncertain(payment, state)
    closed = (
        payment.status != PaymentStatus.PENDING
        or payment.expires_at is None
        or payment.expires_at <= utcnow()
    )
    now = _now()
    active = (
        state is not None
        and not closed
        and not uncertain
        and not state["paid"]
        and state["remote_status"] in {"pending", "need_action"}
    )
    review_open = state is not None and (state.get("review_until") is None or state["review_until"] > now)
    consented = state is not None and state.get("consent_revision") == state["quote"]["revision"]
    result: dict[str, Any] = {
        "payment_id": payment.id,
        "status": payment.status.value,
        "request_amount": payment.amount,
        "payable_amount": state["quote"]["payable_amount"] if state else None,
        "quote_revision": state["quote"]["revision"] if state else None,
        "consented": consented,
        "uncertain": uncertain,
        "recovery": "provider_lookup_unavailable"
        if not payment.provider_ref
        else ("operator_review_required" if uncertain else None),
        "provider_status": state["remote_status"] if state else None,
        "provider_paid": state["paid"] if state else False,
        "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
        "can_consent": bool(active and review_open),
        "can_receipt": bool(
            state is not None
            and active
            and review_open
            and consented
            and state["remote_status"] == "pending"
            and now >= state["created_at"] + state["policy"]["receipt_wait"]
            and (
                not state["receipt_received"]
                or state["remote_status"] == "need_action"
                or state.get("receipt_quote_revision", state["quote"]["revision"])
                != state["quote"]["revision"]
            )
        ),
        "can_change_card": bool(
            state is not None
            and active
            and review_open
            and not state["change_card_exhausted"]
            and (
                state.get("last_change_at") is None
                or now >= state["last_change_at"] + state["policy"]["card_cooldown"]
            )
        ),
        "receipt_ready_at": state["created_at"] + state["policy"]["receipt_wait"] if state else None,
        "receipt_received": bool(state and state["receipt_received"]),
    }
    # The card only after the buyer accepted this exact total, while the invoice is open.
    card = card_of(state) if state is not None and active and review_open and consented else None
    if card is not None:
        result["card"] = card
    return result


async def view(owner: Owner, payment_id: int) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        return projection(payment)


async def recent(owner: Owner) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = list(
            await session.scalars(
                select(Payment)
                .where(
                    Payment.shop_id == owner.shop_id,
                    Payment.customer_id == owner.customer_id,
                    Payment.provider == "tonpay_custom",
                )
                .order_by(Payment.id.desc())
                .limit(20)
            )
        )
        # Do not leak even cached metadata when the principal was banned or shop suspended.
        if rows:
            await _owned(session, owner, rows[0].id)
        return [projection(row) for row in rows]


async def start(owner: Owner, amount: int, key: str) -> dict[str, Any]:
    config, client, confirm = await runtime(owner)
    if not confirm:
        raise CustomStateError("method_disabled")
    result = await create_once(owner, amount, key, config=config, client=client)
    return await view(owner, result.payment_id)


def _epoch(payment: Payment, config: CreationConfig) -> None:
    attempt = (payment.meta or {}).get(CREATE_KEY)
    if (
        not isinstance(attempt, dict)
        or attempt.get("request", {}).get("credential_epoch") != config.credential_epoch
    ):
        raise CustomStateError("credential_changed")


async def client_for(owner: Owner, payment_id: int) -> TonpayCustomClient:
    config, client, _ = await runtime(owner)
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        _epoch(payment, config)
    return client


async def refresh(owner: Owner, payment_id: int) -> dict[str, Any]:
    config, client, confirm = await runtime(owner)
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        _epoch(payment, config)
        if not payment.provider_ref:
            # There is no documented lookup-by-order endpoint. Never guess or accept a client ref.
            return projection(payment)
        state = _state(payment)
        state["read_started_at"] = _now()
        _save(payment, state)
        version = state["version"]
        reference, amount = payment.provider_ref, payment.amount
        await session.commit()
    invoice = await client.check(reference, str(payment_id), amount)
    async with get_sessionmaker()() as session:
        payment = await _owned(session, owner, payment_id)
        _epoch(payment, config)
        state = _state(payment)
        if (
            state.get("version", 0) != version
            or payment.provider_ref != reference
            or payment.amount != amount
        ):
            raise CustomStateError("observation_stale")
        if (
            invoice.invoice_id != reference
            or invoice.order_id != str(payment_id)
            or invoice.request_amount != amount
        ):
            raise CustomStateError("invalid_response")
        _integer(invoice.payable_amount, minimum=amount)
        latest = await shop_settings.load(session, owner.shop_id, fresh=True)
        current_config = (
            latest.get(PREFIX + "enabled") is True
            and latest.get(PREFIX + "policy_approved") is True
            and latest.get(PREFIX + "credential_epoch") == config.credential_epoch
        )
        confirm = confirm and current_config and latest.get(PREFIX + "confirm_enabled") is True
        uncertain = _uncertain(payment, state)
        if not uncertain:
            card_hash = (
                hashlib.sha256((invoice.card_number + "\0" + invoice.card_name).encode()).hexdigest()
                if invoice.card_number
                else None
            )
            changed = invoice.payable_amount != state["quote"]["payable_amount"] or (
                card_hash is not None and state.get("card_fingerprint") not in (None, card_hash)
            )
            if changed:
                state["quote"] = {
                    **state["quote"],
                    "revision": state["quote"]["revision"] + 1,
                    "payable_amount": invoice.payable_amount,
                }
                state["consent_revision"] = None
            if card_hash is not None:
                state["card_fingerprint"] = card_hash
            # A check without a status says nothing new about it (TonPays omits it at times).
            if not state["paid"] and invoice.status:
                state["remote_status"] = invoice.status
            if invoice.status == "processing":
                state["receipt_received"] = True  # TonPays is reviewing a receipt
            keep_card(state, invoice)
            state["paid"] = state["paid"] or invoice.paid
            state["receipt_received"] = state["receipt_received"] or invoice.receipt_received
            state["change_card_exhausted"] = state["change_card_exhausted"] or invoice.change_card_exhausted
            state["last_checked_at"] = _now()
            _save(payment, state)
            # Separate explicit owner gate. GET-derived provider confirmation, NEVER receipt/callback authority.
            if (
                confirm
                and invoice.paid is True
                and invoice.status == "completed"
                and state.get("consent_revision") == state["quote"]["revision"]
                and invoice.payable_amount == state["quote"]["payable_amount"]
                and payment.status == PaymentStatus.PENDING
                and payment.expires_at is not None
                and payment.expires_at > utcnow()
            ):
                from paurubot.payments import engine

                paid = await engine.mark_paid(
                    session, payment.id, provider_tx_id="custom:" + reference, amount=payment.amount
                )
                if paid:
                    await record(
                        session,
                        shop_id=owner.shop_id,
                        actor=Actor.system(),
                        action="tonpay_custom.confirmed",
                        entity="payment",
                        entity_id=payment.id,
                        after={"request_amount": amount, "payable_amount": invoice.payable_amount},
                    )
        # An authenticated read cannot attribute a past receipt/card effect to an uncertain operation.
        # Keep ALL uncertain markers; no false replay success or automatic re-POST.
        await session.commit()
        result = projection(payment)
        if not current_config:
            result.pop("card", None)
        return result


async def resume(owner: Owner, amount: int, key: str) -> dict[str, Any]:
    import re

    _integer(amount)
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise CustomStateError("invalid_key")
    storage = (
        "custom:create:v1:"
        + hashlib.sha256(f"{owner.shop_id}:{owner.customer_id}:{key}".encode()).hexdigest()
    )
    async with get_sessionmaker()() as session:
        pid = await session.scalar(
            select(Payment.id).where(
                Payment.idempotency_key == storage,
                Payment.shop_id == owner.shop_id,
                Payment.customer_id == owner.customer_id,
            )
        )
        if pid is None:
            raise CustomStateError("not_found")
        payment = await _owned(session, owner, pid)
        if payment.amount != amount:
            raise CustomStateError("intent_conflict")
        return projection(payment)
