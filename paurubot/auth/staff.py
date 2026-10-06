from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.auth import sessions
from paurubot.db.base import utcnow
from paurubot.db.models import ResellerProfile, Staff, StaffRole
from paurubot.security import login_guard
from paurubot.security.tokens import hash_password, verify_password


class AuthError(Exception):
    pass


async def authenticate(session: AsyncSession, username: str, password: str, ip: str) -> Staff:
    """Check credentials behind the brute-force guard. Raises AuthError or LoginLocked.

    Staff usernames are unique across all shops, so login needs no shop selector.
    """
    username = username.strip()
    await login_guard.check(username, ip)
    staff = await session.scalar(select(Staff).where(func.lower(Staff.username) == username.lower()))
    shop_id = staff.shop_id if staff else None
    # Always run bcrypt so a missing account costs the same time as a wrong password.
    password_ok = verify_password(password, staff.password_hash if staff else None)
    if staff is None or not staff.is_active or not password_ok:
        await login_guard.failed(username, ip)
        await record(
            session,
            shop_id=shop_id,
            actor=Actor("anonymous", None, ip),
            action="auth.login_failed",
            entity="staff",
            entity_id=staff.id if staff else None,
        )
        raise AuthError("invalid credentials")
    await login_guard.succeeded(username, ip)
    staff.last_login_at = utcnow()
    await record(
        session,
        shop_id=shop_id,
        actor=Actor("staff", staff.id, ip),
        action="auth.login",
        entity="staff",
        entity_id=staff.id,
    )
    return staff


async def create_staff(
    session: AsyncSession,
    shop_id: int,
    *,
    role: StaffRole,
    username: str | None,
    password: str | None,
    telegram_id: int | None = None,
    display_name: str | None = None,
    reseller_profile_id: int | None = None,
) -> Staff:
    if username and await session.scalar(
        select(Staff.id).where(func.lower(Staff.username) == username.strip().lower())
    ):
        raise AuthError("username already taken")
    staff = Staff(
        shop_id=shop_id,
        role=role,
        username=username.strip() if username else None,
        password_hash=hash_password(password) if password else None,
        telegram_id=telegram_id,
        display_name=display_name,
        reseller_profile_id=reseller_profile_id,
    )
    session.add(staff)
    await session.flush()
    return staff


async def change_password(session: AsyncSession, staff: Staff, new_password: str) -> None:
    staff.password_hash = hash_password(new_password)
    staff.session_version += 1
    await session.flush()
    await sessions.destroy_all(staff.id)


async def resolve(session: AsyncSession, data: sessions.StaffSession) -> Staff | None:
    """Load the staff row for a session; stale session versions and disabled accounts are rejected."""
    staff = await session.get(Staff, data.staff_id)
    if staff is None or not staff.is_active or staff.shop_id != data.shop_id:
        return None
    if staff.session_version != data.session_version:
        return None
    return staff


async def reseller_allow_list(session: AsyncSession, staff: Staff) -> list[str] | None:
    if staff.role != StaffRole.RESELLER or staff.reseller_profile_id is None:
        return None
    profile = await session.get(ResellerProfile, staff.reseller_profile_id)
    if profile is None or not profile.is_current:
        return []
    return list(profile.permissions or [])


async def by_telegram(session: AsyncSession, shop_id: int, telegram_id: int) -> Staff | None:
    return await session.scalar(
        select(Staff).where(
            Staff.shop_id == shop_id, Staff.telegram_id == telegram_id, Staff.is_active.is_(True)
        )
    )
