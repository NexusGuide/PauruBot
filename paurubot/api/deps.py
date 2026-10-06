from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor
from paurubot.auth import rbac, sessions
from paurubot.auth import staff as staff_service
from paurubot.db.models import Staff
from paurubot.db.session import get_sessionmaker
from paurubot.security.login_guard import client_ip
from paurubot.security.tokens import constant_equals

SESSION_COOKIE = "pb_session"
CSRF_COOKIE = "pb_csrf"
CSRF_HEADER = "x-csrf-token"


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


DB = Annotated[AsyncSession, Depends(get_db)]


def request_ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    return client_ip(peer, request.headers.get("x-forwarded-for"))


@dataclass(slots=True)
class StaffContext:
    staff: Staff
    shop_id: int
    session: sessions.StaffSession
    session_id: str
    ip: str
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def scoped(self) -> bool:
        return rbac.is_scoped(self.staff.role)

    @property
    def actor(self) -> Actor:
        return Actor("staff", self.staff.id, self.ip)

    def can(self, permission: str) -> bool:
        return permission in self.permissions


async def _api_key_staff(request: Request, db: AsyncSession, token: str) -> StaffContext:
    resolved = await staff_service.resolve_api_key(db, token)
    if resolved is None:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    staff, key = resolved
    if key.read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
        raise HTTPException(status_code=403, detail="read_only_key")
    allow_list = await staff_service.reseller_allow_list(db, staff)
    perms = frozenset(rbac.permissions_for(staff.role, allow_list))
    # Keys never manage staff or other keys, whatever the role allows.
    perms = perms - {"staff.manage", "panels.secrets", "backups.manage"}
    session = sessions.StaffSession(
        staff.id, staff.shop_id, staff.session_version, "", 0.0, request_ip(request)
    )
    return StaffContext(staff, staff.shop_id, session, f"apikey:{key.id}", request_ip(request), perms)


async def current_staff(request: Request, db: DB) -> StaffContext:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return await _api_key_staff(request, db, authorization[7:].strip())
    session_id = request.cookies.get(SESSION_COOKIE)
    data = await sessions.load(session_id)
    if data is None or session_id is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    staff = await staff_service.resolve(db, data)
    if staff is None:
        await sessions.destroy(session_id)
        raise HTTPException(status_code=401, detail="session_revoked")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not constant_equals(request.headers.get(CSRF_HEADER), data.csrf):
            raise HTTPException(status_code=403, detail="csrf_failed")
    allow_list = await staff_service.reseller_allow_list(db, staff)
    perms = frozenset(rbac.permissions_for(staff.role, allow_list))
    return StaffContext(staff, staff.shop_id, data, session_id, request_ip(request), perms)


CurrentStaff = Annotated[StaffContext, Depends(current_staff)]


def require(permission: str) -> Callable[..., Coroutine[Any, Any, StaffContext]]:
    if permission not in rbac.PERMISSIONS:
        raise ValueError(f"unknown permission {permission}")

    async def checker(ctx: CurrentStaff) -> StaffContext:
        if not ctx.can(permission):
            raise HTTPException(status_code=403, detail="forbidden")
        return ctx

    return checker
