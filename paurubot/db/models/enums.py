from __future__ import annotations

import enum


class ShopStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class StaffRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    FINANCE = "finance"
    SUPPORT = "support"
    RESELLER = "reseller"


class PanelKind(enum.StrEnum):
    PASARGUARD = "pasarguard"
    MARZBAN = "marzban"
    XUI = "xui"
    MARZNESHIN = "marzneshin"


class UsernameMethod(enum.StrEnum):
    TELEGRAM_ID = "telegram_id"
    TELEGRAM_USERNAME = "telegram_username"
    RANDOM = "random"
    SEQUENTIAL = "sequential"
    CUSTOM_LABEL = "custom_label"


class PlanAudience(enum.StrEnum):
    ALL = "all"
    CUSTOMERS = "customers"
    RESELLERS = "resellers"


class ServiceStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    LIMITED = "limited"
    EXPIRED = "expired"
    DISABLED = "disabled"
    DELETED = "deleted"


class OrderKind(enum.StrEnum):
    PURCHASE = "purchase"
    RENEW = "renew"
    EXTRA_VOLUME = "extra_volume"
    EXTRA_DAYS = "extra_days"
    UPGRADE = "upgrade"
    TRIAL = "trial"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    PROVISIONING = "provisioning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class PaymentStatus(enum.StrEnum):
    CREATED = "created"
    PENDING = "pending"
    VERIFYING = "verifying"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


class PaymentPurpose(enum.StrEnum):
    TOPUP = "topup"
    ORDER = "order"


class LedgerAccountKind(enum.StrEnum):
    WALLET = "wallet"
    REVENUE = "revenue"
    GATEWAY = "gateway"
    PROMOTIONS = "promotions"
    ADJUSTMENTS = "adjustments"
    REFUNDS = "refunds"
    BANK = "bank"
    COMMISSIONS = "commissions"


class JobStatus(enum.StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
    CANCELED = "canceled"


class TicketStatus(enum.StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class ApplicationStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DiscountKind(enum.StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"
