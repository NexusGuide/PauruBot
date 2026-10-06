"""Panel management (PNL-*): credentials are write-only, certificates are pinned explicitly."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select

from paurubot.api.admin_common import owned, plain
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, PanelKind, Service, ServiceStatus, UsernameMethod
from paurubot.panels import PanelError, drop, registry
from paurubot.panels.drivers.pasarguard import ADMIN_READ_PATHS, PasarGuardDriver
from paurubot.panels.http import fetch_certificate
from paurubot.security import secretbox, ssrf
from paurubot.security.tokens import new_token

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("panels.read"))]
Writer = Annotated[StaffContext, Depends(require("panels.write"))]

PANEL_FIELDS = [
    "id",
    "name",
    "kind",
    "base_url",
    "username",
    "location_id",
    "capacity",
    "username_method",
    "username_prefix",
    "is_active",
    "is_healthy",
    "health_checked_at",
    "health_error",
    "allow_private_address",
    "created_at",
    "updated_at",
]
SAFE_OPTION_KEYS = {
    "group_ids",
    "inbounds",
    "proxies",
    "flow",
    "inbound_id",
    "sub_base_url",
    "limit_ip",
    "template_id",
    "legacy_paths",
    "timeout",
}


def panel_out(panel: Panel, load: int | None = None) -> dict[str, Any]:
    data = plain(panel, PANEL_FIELDS)
    data["options"] = {k: v for k, v in (panel.options or {}).items() if k in SAFE_OPTION_KEYS}
    data["has_api_token"] = bool((panel.options or {}).get("api_token_enc"))
    data["tls_pinned"] = bool(panel.tls_cert_pem)
    data["webhook_enabled"] = bool(panel.webhook_secret_enc)
    if load is not None:
        data["load"] = load
    return data


class PanelBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: PanelKind
    base_url: HttpUrl
    username: str = Field(min_length=1, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    api_token: str | None = Field(default=None, max_length=512)
    location_id: int | None = None
    capacity: int | None = Field(default=None, ge=1)
    username_method: UsernameMethod = UsernameMethod.TELEGRAM_ID
    username_prefix: str = Field(default="", max_length=16, pattern=r"^[A-Za-z0-9_]*$")
    options: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    allow_private_address: bool = False


def _clean_options(options: dict[str, Any]) -> dict[str, Any]:
    unknown = set(options) - SAFE_OPTION_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown_options:{','.join(sorted(unknown))}")
    return options


async def _check_url(body: PanelBody, ctx: StaffContext) -> str:
    url = str(body.base_url).rstrip("/")
    if body.allow_private_address and not ctx.can("panels.secrets"):
        raise HTTPException(status_code=403, detail="private_address_needs_owner")
    try:
        ssrf.check_static(url, allow_private=body.allow_private_address)
    except ssrf.UnsafeUrl as exc:
        raise HTTPException(status_code=422, detail="unsafe_url") from exc
    return url


@router.get("/panels")
async def list_panels(ctx: Reader, db: DB) -> dict[str, Any]:
    panels = list(await db.scalars(select(Panel).where(Panel.shop_id == ctx.shop_id).order_by(Panel.id)))
    loads = dict(
        (
            await db.execute(
                select(Service.panel_id, func.count(Service.id))
                .where(Service.shop_id == ctx.shop_id, Service.status != ServiceStatus.DELETED)
                .group_by(Service.panel_id)
            )
        ).all()
    )
    return {"items": [panel_out(p, int(loads.get(p.id, 0))) for p in panels]}


@router.post("/panels")
async def create_panel(body: PanelBody, ctx: Writer, db: DB) -> dict[str, Any]:
    if not body.password:
        raise HTTPException(status_code=422, detail="password_required")
    url = await _check_url(body, ctx)
    options = _clean_options(dict(body.options))
    if body.api_token:
        options["api_token_enc"] = secretbox.encrypt("panel", body.api_token)
    panel = Panel(
        shop_id=ctx.shop_id,
        name=body.name,
        kind=body.kind,
        base_url=url,
        username=body.username,
        password_enc=secretbox.encrypt("panel", body.password),
        location_id=body.location_id,
        capacity=body.capacity,
        username_method=body.username_method,
        username_prefix=body.username_prefix,
        options=options,
        is_active=body.is_active,
        allow_private_address=body.allow_private_address,
    )
    db.add(panel)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.create",
        entity="panel",
        entity_id=panel.id,
        after=snapshot(panel),
    )
    return panel_out(panel)


@router.put("/panels/{panel_id}")
async def update_panel(panel_id: int, body: PanelBody, ctx: Writer, db: DB) -> dict[str, Any]:
    panel = await owned(db, Panel, panel_id, ctx)
    before = snapshot(panel)
    url = await _check_url(body, ctx)
    secrets_changing = bool(body.password or body.api_token) or url != panel.base_url
    if secrets_changing and not ctx.can("panels.secrets"):
        raise HTTPException(status_code=403, detail="secrets_need_owner")
    options = _clean_options(dict(body.options))
    if (panel.options or {}).get("api_token_enc"):
        options["api_token_enc"] = panel.options["api_token_enc"]
    if body.api_token:
        options["api_token_enc"] = secretbox.encrypt("panel", body.api_token)
    if url != panel.base_url:
        panel.tls_cert_pem = None  # a pinned certificate belongs to the old address
    panel.name, panel.kind, panel.base_url, panel.username = body.name, body.kind, url, body.username
    if body.password:
        panel.password_enc = secretbox.encrypt("panel", body.password)
    panel.location_id, panel.capacity = body.location_id, body.capacity
    panel.username_method, panel.username_prefix = body.username_method, body.username_prefix
    panel.options, panel.is_active = options, body.is_active
    panel.allow_private_address = body.allow_private_address
    panel.updated_at = utcnow()
    await drop(panel.id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.update",
        entity="panel",
        entity_id=panel.id,
        before=before,
        after=snapshot(panel),
    )
    return panel_out(panel)


@router.post("/panels/{panel_id}/test")
async def test_panel(panel_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    panel = await owned(db, Panel, panel_id, ctx)
    await drop(panel.id)
    try:
        driver = await registry.driver_for(panel)
        health = await driver.health_check()
        options = [{"id": o.id, "title": o.title} for o in await driver.list_options()]
    except PanelError as exc:
        panel.is_healthy, panel.health_error, panel.health_checked_at = False, str(exc)[:255], utcnow()
        return {"ok": False, "error": str(exc)[:255], "status": exc.status}
    panel.is_healthy, panel.health_error, panel.health_checked_at = True, None, utcnow()
    return {
        "ok": True,
        "detail": health.detail,
        "users": health.users,
        "version": health.version,
        "options": options,
    }


class CertificateBody(BaseModel):
    url: HttpUrl
    allow_private: bool = False


@router.post("/panels/certificate")
async def inspect_certificate(
    body: CertificateBody, ctx: Annotated[StaffContext, Depends(require("panels.secrets"))]
) -> dict[str, Any]:
    try:
        pem, fingerprint = await fetch_certificate(str(body.url), allow_private=body.allow_private)
    except (PanelError, ssrf.UnsafeUrl) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc
    return {"pem": pem, "fingerprint": fingerprint}


class PinBody(BaseModel):
    pem: str | None = Field(default=None, max_length=20_000)


@router.post("/panels/{panel_id}/pin")
async def pin_certificate(
    panel_id: int, body: PinBody, ctx: Annotated[StaffContext, Depends(require("panels.secrets"))], db: DB
) -> dict[str, Any]:
    panel = await owned(db, Panel, panel_id, ctx)
    if body.pem and "BEGIN CERTIFICATE" not in body.pem:
        raise HTTPException(status_code=422, detail="not_a_certificate")
    panel.tls_cert_pem = body.pem or None
    panel.updated_at = utcnow()
    await drop(panel.id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.pin" if body.pem else "panel.unpin",
        entity="panel",
        entity_id=panel.id,
    )
    return panel_out(panel)


@router.post("/panels/{panel_id}/webhook")
async def rotate_webhook(
    panel_id: int, ctx: Annotated[StaffContext, Depends(require("panels.secrets"))], db: DB
) -> dict[str, Any]:
    """Create a new webhook secret; it is shown once and stored encrypted."""
    panel = await owned(db, Panel, panel_id, ctx)
    if panel.kind == PanelKind.XUI:
        raise HTTPException(status_code=400, detail="webhooks_not_supported")
    secret = new_token(24)
    panel.webhook_secret_enc = secretbox.encrypt("webhook", secret)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.webhook_rotated",
        entity="panel",
        entity_id=panel.id,
    )
    from paurubot.config import get_settings

    base = get_settings().public_url or ""
    return {"secret": secret, "url": f"{base}/api/webhooks/panel/{panel.id}"}


@router.get("/panels/{panel_id}/pasarguard/{resource}")
async def pasarguard_view(
    panel_id: int,
    resource: str,
    ctx: Reader,
    db: DB,
    offset: int = 0,
    limit: int = 50,
    search: str | None = None,
) -> dict[str, Any]:
    """Read-only window into PasarGuard admin data (PNL-01)."""
    if resource not in ADMIN_READ_PATHS:
        raise HTTPException(status_code=404, detail="not_found")
    panel = await owned(db, Panel, panel_id, ctx)
    driver = await registry.driver_for(panel)
    if not isinstance(driver, PasarGuardDriver):
        raise HTTPException(status_code=400, detail="not_pasarguard")
    try:
        data = await driver.admin_read(
            resource, {"offset": offset, "limit": min(limit, 200), "search": search}
        )
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    return {"data": data}


@router.post("/panels/{panel_id}/pasarguard/nodes/{node_id}/{action}")
async def pasarguard_node(panel_id: int, node_id: int, action: str, ctx: Writer, db: DB) -> dict[str, Any]:
    if action not in {"reconnect", "sync"}:
        raise HTTPException(status_code=404, detail="not_found")
    panel = await owned(db, Panel, panel_id, ctx)
    driver = await registry.driver_for(panel)
    if not isinstance(driver, PasarGuardDriver):
        raise HTTPException(status_code=400, detail="not_pasarguard")
    try:
        if action == "reconnect":
            await driver.node_reconnect(node_id)
        else:
            await driver.node_sync(node_id)
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"panel.node_{action}",
        entity="panel",
        entity_id=panel.id,
        after={"node_id": node_id},
    )
    return {"ok": True}
