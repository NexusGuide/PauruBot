from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from paurubot.api.deps import CSRF_COOKIE, DB, SESSION_COOKIE, CurrentStaff, request_ip
from paurubot.audit.log import record
from paurubot.auth import sessions
from paurubot.auth import staff as staff_service
from paurubot.config import get_settings
from paurubot.security.login_guard import LoginLocked
from paurubot.security.tokens import verify_password

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
        SESSION_COOKIE, session_id, max_age=max_age, httponly=True, secure=secure, samesite="strict", path="/"
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=secure, samesite="strict", path="/"
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
            status_code=429, detail="too_many_attempts", headers={"Retry-After": str(exc.retry_after)}
        ) from exc
    except staff_service.AuthError as exc:
        await db.commit()  # keep the failed-login audit row
        raise HTTPException(status_code=401, detail="invalid_credentials") from exc
    session_id, data = await sessions.create(staff.id, staff.shop_id, staff.session_version, ip)
    _set_cookies(response, session_id, data.csrf)
    from paurubot.auth import rbac

    allow_list = await staff_service.reseller_allow_list(db, staff)
    return _me(staff, rbac.permissions_for(staff.role, allow_list), data.csrf)


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
    if not verify_password(body.current_password, ctx.staff.password_hash):
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
