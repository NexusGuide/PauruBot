from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from paurubot.api.deps import (
    CSRF_COOKIE,
    DB,
    SESSION_COOKIE,
    CurrentStaff,
    StaffContext,
    request_ip,
)
from paurubot.audit.log import record
from paurubot.auth import password_reset, sessions
from paurubot.auth import staff as staff_service
from paurubot.config import get_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Staff, StaffApiKey
from paurubot.security.login_guard import LoginLocked
from paurubot.security.tokens import verify_password_async

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


def _set_cookies(response: Response, session_id: str, csrf: str) -> None:
    settings = get_settings()
    secure = settings.is_production or settings.public_url.startswith("https://")
    max_age = settings.session_ttl_seconds
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def _me(ctx_staff, permissions: list[str], csrf: str) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": ctx_staff.id,
        "shop_id": ctx_staff.shop_id,
        "username": ctx_staff.username,
        "display_name": ctx_staff.display_name,
        "role": ctx_staff.role.value,
        "permissions": permissions,
        "csrf_token": csrf,
    }


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, db: DB) -> dict:
    ip = request_ip(request)
    try:
        staff = await staff_service.authenticate(db, body.username, body.password, ip)
    except LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except staff_service.AuthError as exc:
        await db.commit()  # keep the failed-login audit row
        raise HTTPException(status_code=401, detail="invalid_credentials") from exc
    session_id, data = await sessions.create(staff.id, staff.shop_id, staff.session_version, ip)
    _set_cookies(response, session_id, data.csrf)
    from paurubot.auth import rbac

    allow_list = await staff_service.reseller_allow_list(db, staff)
    return _me(staff, rbac.permissions_for(staff.role, allow_list), data.csrf)


class CodeRequestBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class CodeVerifyBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


@router.post("/code")
async def request_login_code(body: CodeRequestBody, request: Request, db: DB) -> dict:
    """Ask the bot to send a one-time code to the staff member's Telegram (SEC-02)."""
    try:
        await staff_service.request_code(db, body.username, request_ip(request))
    except LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    await db.commit()
    return {"ok": True}  # same answer whether or not the account exists


@router.post("/code/verify")
async def verify_login_code(body: CodeVerifyBody, request: Request, response: Response, db: DB) -> dict:
    ip = request_ip(request)
    try:
        staff = await staff_service.verify_code(db, body.username, body.code, ip)
    except LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except staff_service.AuthError as exc:
        await db.commit()
        raise HTTPException(status_code=401, detail="invalid_code") from exc
    session_id, data = await sessions.create(staff.id, staff.shop_id, staff.session_version, ip)
    _set_cookies(response, session_id, data.csrf)
    from paurubot.auth import rbac

    allow_list = await staff_service.reseller_allow_list(db, staff)
    return _me(staff, rbac.permissions_for(staff.role, allow_list), data.csrf)


class ApiKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    read_only: bool = True
    expires_at: datetime | None = None


def _key_out(row: StaffApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "read_only": row.read_only,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def _cookie_only(ctx: StaffContext) -> None:
    if ctx.session_id.startswith("apikey:"):
        raise HTTPException(status_code=403, detail="needs_browser_session")


@router.get("/api-keys")
async def list_api_keys(ctx: CurrentStaff, db: DB) -> dict:
    _cookie_only(ctx)
    rows = await db.scalars(
        select(StaffApiKey).where(StaffApiKey.staff_id == ctx.staff.id).order_by(StaffApiKey.id.desc())
    )
    return {"items": [_key_out(r) for r in rows]}


@router.post("/api-keys")
async def create_api_key(body: ApiKeyBody, ctx: CurrentStaff, db: DB) -> dict:
    _cookie_only(ctx)
    active = await db.scalar(
        select(func.count(StaffApiKey.id)).where(
            StaffApiKey.staff_id == ctx.staff.id, StaffApiKey.revoked_at.is_(None)
        )
    )
    if int(active or 0) >= 10:
        raise HTTPException(status_code=409, detail="too_many_keys")
    row, key = await staff_service.create_api_key(
        db, ctx.staff, body.name, read_only=body.read_only, expires_at=body.expires_at
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="auth.api_key_created",
        entity="staff_api_key",
        entity_id=row.id,
        after={"name": row.name, "read_only": row.read_only},
    )
    return {"key": key, "item": _key_out(row)}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: int, ctx: CurrentStaff, db: DB) -> dict:
    _cookie_only(ctx)
    row = await db.get(StaffApiKey, key_id)
    if row is None or row.staff_id != ctx.staff.id:
        raise HTTPException(status_code=404, detail="not_found")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="auth.api_key_revoked",
            entity="staff_api_key",
            entity_id=row.id,
        )
    return {"item": _key_out(row)}


@router.post("/logout")
async def logout(ctx: CurrentStaff, response: Response) -> dict:
    await sessions.destroy(ctx.session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(ctx: CurrentStaff) -> dict:
    return _me(ctx.staff, sorted(ctx.permissions), ctx.session.csrf)


@router.post("/password")
async def change_password(body: PasswordBody, ctx: CurrentStaff, response: Response, db: DB) -> dict:
    _cookie_only(ctx)
    locked = await db.scalar(
        select(Staff)
        .where(Staff.id == ctx.staff.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None or not locked.is_active or locked.session_version != ctx.session.session_version:
        raise HTTPException(status_code=401, detail="session_revoked")
    if not await verify_password_async(body.current_password, ctx.staff.password_hash):
        raise HTTPException(status_code=400, detail="wrong_password")
    await staff_service.change_password(db, ctx.staff, body.new_password)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="auth.password_changed",
        entity="staff",
        entity_id=ctx.staff.id,
    )
    session_id, data = await sessions.create(ctx.staff.id, ctx.shop_id, ctx.staff.session_version, ctx.ip)
    _set_cookies(response, session_id, data.csrf)
    return {"ok": True, "csrf_token": data.csrf}


class ResetRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)


class ResetConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=128)
    new_password: str = Field(min_length=10, max_length=256)


@router.post("/password-reset/request")
async def request_password_reset(body: ResetRequestBody, request: Request, db: DB) -> dict:
    try:
        await password_reset.request(db, body.username, request_ip(request))
    except LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    return {"ok": True}


@router.post("/password-reset/confirm")
async def confirm_password_reset(body: ResetConfirmBody, request: Request, db: DB) -> dict:
    try:
        await password_reset.confirm(db, body.token, body.new_password, request_ip(request))
    except LoginLocked as exc:
        raise HTTPException(
            status_code=429,
            detail="too_many_attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except password_reset.ResetError as exc:
        raise HTTPException(status_code=400, detail="invalid_reset") from exc
    return {"ok": True}
