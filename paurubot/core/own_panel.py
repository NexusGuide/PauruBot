"""A dedicated reseller bot on the reseller's own panel (OPB).

A customer who bought a PasarGuard admin account (TEN-14) can be given its own Telegram bot,
connected by the shop's staff. That bot serves a child shop that sells the reseller's OWN plans:
configs are made on the purchased admin account, and buyers pay the reseller directly (its card),
so nothing is ever taken from the reseller's wallet in the main shop.

The child shop has no reseller profile, so the shop-panels mode of reseller bots (TEN-04,
`core/resale.py`) never applies to it: its catalog, panels, settings, customers and payments are
its own. It is linked to the admin account by `Shop.own_account_id`, and its one panel logs in with
that account's own credentials, so the panel itself keeps every sale inside the account's limits.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Customer,
    Panel,
    PanelAccount,
    PanelKind,
    Shop,
    ShopStatus,
    Staff,
    StaffRole,
    UsernameMethod,
)

# Purchases and the card are on from the start; the reseller turns the rest on for its own buyers.
STARTING_SETTINGS = {
    "bot_enabled": True,
    "card_enabled": True,
    "trial_enabled": False,
    "extras_enabled": False,
    "upgrade_enabled": False,
    "referral_enabled": False,
    "panel_accounts_enabled": False,
    "guest_buy_enabled": False,
    "stars_enabled": False,
}
# Panel kinds whose admin accounts can create and manage their own users.
KINDS = {PanelKind.PASARGUARD}


class OwnPanelError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def is_own(shop: Shop | None) -> bool:
    return shop is not None and shop.own_account_id is not None


async def shop_for_customer(session: AsyncSession, customer: Customer) -> Shop | None:
    """The dedicated own-panel bot shop of this customer, connected or not."""
    return await session.scalar(
        select(Shop)
        .join(PanelAccount, PanelAccount.id == Shop.own_account_id)
        .where(PanelAccount.customer_id == customer.id, PanelAccount.shop_id == customer.shop_id)
        .limit(1)
    )


async def eligible_accounts(session: AsyncSession, customer: Customer) -> list[PanelAccount]:
    """The customer's purchased admin accounts on a panel kind that can serve a bot, newest first."""
    rows = await session.scalars(
        select(PanelAccount)
        .where(
            PanelAccount.customer_id == customer.id,
            PanelAccount.shop_id == customer.shop_id,
            PanelAccount.status != "deleted",
        )
        .order_by(PanelAccount.id.desc())
    )
    return [a for a in rows if a.panel is not None and a.panel.kind in KINDS]


def usable(account: PanelAccount) -> bool:
    """An account that may be linked now (selling is gated separately, by its live limits)."""
    return account.status == "active" and account.panel is not None and account.panel.kind in KINDS


async def _own_panel(session: AsyncSession, shop: Shop, account: PanelAccount) -> Panel:
    """The child shop's one panel: the purchased admin account's own login on the same server."""
    source = account.panel
    assert source is not None
    panel = await session.scalar(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id).limit(1))
    if panel is None:
        panel = Panel(
            shop_id=shop.id,
            name=account.username[:64],
            kind=source.kind,
            base_url=source.base_url,
            username=account.username,
            password_enc=account.password_enc,
            username_method=UsernameMethod.CUSTOMER_NAME,
            username_prefix="",
        )
        session.add(panel)
    panel.kind, panel.base_url = source.kind, source.base_url
    panel.username, panel.password_enc = account.username, account.password_enc
    panel.tls_cert_pem, panel.allow_private_address = source.tls_cert_pem, source.allow_private_address
    panel.is_active = True
    await session.flush()
    return panel


async def follow_credentials(session: AsyncSession, account: PanelAccount) -> None:
    """The admin account's password changed: its dedicated bot's panel logs in with the new one."""
    shop = await session.scalar(select(Shop).where(Shop.own_account_id == account.id))
    if shop is None:
        return
    panel = await session.scalar(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id).limit(1))
    if panel is not None and panel.username == account.username:
        panel.password_enc = account.password_enc  # the driver cache keys on it, so it re-logs in


async def check_login(panel: Panel) -> list[str]:
    """Log in with the admin account and read the groups it may use; refused: OwnPanelError."""
    from paurubot.panels import PanelError, driver_for

    try:
        driver = await driver_for(panel)
        options = await driver.list_options()
    except PanelError as exc:
        raise OwnPanelError("own_panel_unreachable") from exc
    return [option.title for option in options]


async def connect(
    session: AsyncSession, account: PanelAccount, token: str, title: str = "", *, verify: bool = True
) -> Shop:
    """Create (or re-point) the customer's dedicated bot shop on `account`, with bot `token`.

    Staff only. The token is checked with Telegram, the account's login with its panel."""
    from paurubot.auth.staff import create_staff
    from paurubot.config import get_settings
    from paurubot.core import resale
    from paurubot.notifications.telegram import bot_id_of, forget_shop_bot
    from paurubot.security import secretbox

    if not usable(account):
        raise OwnPanelError("own_panel_account_unusable")
    owner = await session.get(Customer, account.customer_id)
    parent = await session.get(Shop, account.shop_id)
    if owner is None or parent is None or parent.parent_id is not None:
        raise OwnPanelError("own_panel_account_unusable")
    token = token.strip()
    if not re.match(resale.TOKEN_SHAPE, token):
        raise OwnPanelError("bot_token_invalid")
    if bot_id_of(token) == bot_id_of(get_settings().bot_token.get_secret_value()):
        raise OwnPanelError("bot_token_is_main")
    try:
        bot_id, username = await resale.bot_identity(token)
    except resale.ResaleError as exc:
        raise OwnPanelError(exc.key) from exc
    shop = await shop_for_customer(session, owner)
    taken = await session.scalar(select(Shop).where(Shop.bot_user_id == bot_id))
    if taken is not None and (shop is None or taken.id != shop.id):
        raise OwnPanelError("bot_token_in_use")
    if shop is None:
        shop = Shop(
            parent_id=parent.id,
            title=title.strip()[:96] or username,
            currency=parent.currency,
            default_locale=parent.default_locale,
        )
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(session, shop.id, STARTING_SETTINGS)
    elif title.strip():
        shop.title = title.strip()[:96]
    shop.own_account_id = account.id
    panel = await _own_panel(session, shop, account)
    if verify:
        await check_login(panel)
    shop.bot_token_enc = secretbox.encrypt("bot-token", token)
    shop.bot_user_id, shop.bot_username, shop.status = bot_id, username, ShopStatus.ACTIVE
    merchant = await session.scalar(
        select(Staff).where(Staff.shop_id == shop.id, Staff.telegram_id == owner.telegram_id)
    )
    if merchant is None:
        await create_staff(
            session,
            shop.id,
            role=StaffRole.MERCHANT,
            username=None,
            password=None,
            telegram_id=owner.telegram_id,
            display_name=owner.display_name,
        )
    else:
        merchant.is_active = True
    await session.flush()
    forget_shop_bot(shop.id)
    return shop


async def disconnect(session: AsyncSession, shop: Shop) -> None:
    """Stop the bot. Its customers, services, payments and plans stay; connecting again brings
    them back."""
    from paurubot.notifications.telegram import forget_shop_bot

    shop.status = ShopStatus.SUSPENDED
    shop.bot_token_enc = None
    shop.bot_user_id = None
    forget_shop_bot(shop.id)
    await session.flush()


# --- selling only while the admin account can serve ---------------------------------------
# The panel itself refuses what the account cannot hold, but a buyer must never pay for that: the
# bot stops new sales and trials as soon as the account cannot serve, and opens them again once
# it can, after one live login. Existing services, payments and records are never touched.

PAUSED = "own_sales_paused"  # "" while selling, else why: inactive|expired|traffic_full|users_full
HOLD = "own_sales_hold"  # a manual pause by the main shop's staff


def blocking_reason(account: PanelAccount | None) -> str:
    from paurubot.db.base import utcnow

    if account is None or account.status == "deleted":
        return "inactive"
    if account.status == "expired" or (account.expire_at is not None and account.expire_at <= utcnow()):
        return "expired"
    if account.status != "active":
        return "inactive"
    if account.data_limit and account.used_bytes >= account.data_limit:
        return "traffic_full"
    if account.max_users and account.users_count >= account.max_users:
        return "users_full"
    return ""


async def sales_gate(session: AsyncSession, shop_id: int, *, new_user: bool) -> str | None:
    """None when this shop may take the order now, else the locale key telling the buyer why not.

    `new_user`: a purchase or a trial makes a user on the panel; renewals and extras do not, so a
    full user capacity alone never stops them."""
    shop = await session.get(Shop, shop_id)
    if not is_own(shop):
        return None
    cfg = await shop_settings.load(session, shop_id)
    if cfg.get(HOLD):
        return "own_sales_paused"
    reason = str(cfg.get(PAUSED) or "")
    if not reason or (reason == "users_full" and not new_user):
        return None
    return "own_sales_paused"


async def recheck(session: AsyncSession, shop: Shop, *, verify: bool = True) -> str | None:
    """Recompute whether `shop` may sell from its account; returns the alert for a change, if any.

    Opening sales again needs one live login, so an account that only looks usable stays paused."""
    account = await session.get(PanelAccount, shop.own_account_id) if shop.own_account_id else None
    reason = blocking_reason(account)
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    old = str(cfg.get(PAUSED) or "")
    if reason == old:
        return None
    if not reason and verify:
        panel = await session.scalar(
            select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id).limit(1)
        )
        try:
            if panel is None:
                raise OwnPanelError("own_panel_unreachable")
            await check_login(panel)
        except OwnPanelError:
            return None  # tried again on the next pass
    await shop_settings.set_many(session, shop.id, {PAUSED: reason})
    return f"own_paused_{reason}" if reason else "own_resumed"


async def recheck_all(session: AsyncSession) -> list[tuple[Shop, str]]:
    """Every dedicated bot's selling state; returns (shop, alert) for each change."""
    changes: list[tuple[Shop, str]] = []
    for shop in list(await session.scalars(select(Shop).where(Shop.own_account_id.is_not(None)))):
        alert = await recheck(session, shop)
        if alert:
            changes.append((shop, alert))
    return changes


async def recheck_account(session: AsyncSession, account: PanelAccount) -> list[tuple[Shop, str]]:
    """Right after the account was renewed or topped up: its bot sells again without waiting."""
    shop = await session.scalar(select(Shop).where(Shop.own_account_id == account.id))
    if shop is None:
        return []
    alert = await recheck(session, shop)
    return [(shop, alert)] if alert else []


async def announce(session: AsyncSession, changes: list[tuple[Shop, str]]) -> None:
    """Queue the alerts for both bots: the reseller's own and the main shop's."""
    from paurubot.worker import queue

    for shop, alert in changes:
        await queue.enqueue(
            session,
            "notify",
            "own_panel_alert",
            {"shop_id": shop.id, "alert": alert},
            shop_id=shop.id,
        )
