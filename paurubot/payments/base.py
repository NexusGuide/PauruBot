"""Payment provider protocol (PAY-01)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from paurubot.db.models import Payment


class PaymentError(Exception):
    """Carries a locale key for the customer and optional format parameters."""

    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(slots=True)
class Checkout:
    """What the customer needs to complete a payment."""

    redirect_url: str | None = None
    instructions: dict[str, Any] = field(default_factory=dict)
    provider_ref: str | None = None
    provider_amount: int | None = None
    provider_currency: str | None = None


@dataclass(slots=True)
class Verification:
    paid: bool
    provider_tx_id: str | None = None
    amount: int | None = None
    currency: str | None = None
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(ABC):
    name: str = ""
    manual_review: bool = False

    @abstractmethod
    async def enabled(self, cfg: dict[str, Any]) -> bool: ...

    @abstractmethod
    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout: ...

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        raise PaymentError("method_disabled")

    async def refund(self, payment: Payment, cfg: dict[str, Any]) -> bool:
        return False
