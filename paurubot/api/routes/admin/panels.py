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
from paurubot.db.models import (
    Customer,
    Location,
    Panel,
    PanelKind,
    Service,
    ServiceStatus,
    UsernameMethod,
)
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
    "service_ids",
    "squad_ids",
    "sell_extra_volume",
    "sell_extra_days",
    "renew_resets_usage",
    "legacy_paths",
    "guard_version",
    "timeout",
    "configuration",
    "template_user",
    "profile",
    "group",
}

# Option values that end up in panel API paths or forms: short names only.
NAME_OPTIONS = {
    "configuration": r"^[A-Za-z0-9_.-]{1,32}$",
    "template_user": r"^[A-Za-z0-9_.@-]{1,64}$",
    "profile": r"^[^\x00-\x1f/?#]{1,64}$",
    "group": r"^[^\x00-\x1f/?#&]{1,64}$",
}


# Panels that can be reached with an API token alone, without a username and password.
TOKEN_PANELS = {
    PanelKind.REMNAWAVE,
    PanelKind.REBECCA,
    PanelKind.GUARD,
    PanelKind.HIDDIFY,
    PanelKind.WGDASHBOARD,
    PanelKind.WGMATE,
}


def panel_out(panel: Panel, load: int | None = None) -> dict[str, Any]:
    data = plain(panel, PANEL_FIELDS)
    data["options"] = {k: v for k, v in (panel.options or {}).items() if k in SAFE_OPTION_KEYS}
    data["has_api_token"] = bool((panel.options or {}).get("api_token_enc"))
    data["tls_pinned"] = bool(panel.tls_cert_pem)
    data["webhook_enabled"] = bool(panel.webhook_secret_enc)
    data["stats"] = panel.stats or {}
    data["stats_at"] = panel.stats_at.isoformat() if panel.stats_at else None
    data["down_since"] = panel.down_since.isoformat() if panel.down_since else None
    if load is not None:
        data["load"] = load
    return data


class PanelBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: PanelKind
    base_url: HttpUrl
    username: str = Field(default="", max_length=128)
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
    for key in ("sell_extra_volume", "sell_extra_days", "renew_resets_usage"):
        if key in options and options[key] is not None and not isinstance(options[key], bool):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")
    if options.get("guard_version") not in (None, "v1", "v2"):
        raise HTTPException(status_code=422, detail="guard_version_invalid")
    import re

    for key, pattern in NAME_OPTIONS.items():
        value = options.get(key)
        if value not in (None, "") and not re.match(pattern, str(value)):
            raise HTTPException(status_code=422, detail=f"{key}_invalid")
    return {k: v for k, v in options.items() if v is not None}


async def _check_location(db: DB, body: PanelBody, ctx: StaffContext) -> None:
    if body.location_id is not None:
        await owned(db, Location, body.location_id, ctx)  # never another shop's location


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
    from paurubot.provisioning.monitor import uptime

    up = await uptime(db, [p.id for p in panels])
    return {"items": [{**panel_out(p, int(loads.get(p.id, 0))), "uptime": up.get(p.id)} for p in panels]}


@router.post("/panels")
async def create_panel(body: PanelBody, ctx: Writer, db: DB) -> dict[str, Any]:
    token_only = body.kind in TOKEN_PANELS and bool(body.api_token)
    if not body.password and not token_only:
        raise HTTPException(status_code=422, detail="password_required")
    if not body.username.strip() and not token_only:
        raise HTTPException(status_code=422, detail="username_required")
    url = await _check_url(body, ctx)
    await _check_location(db, body, ctx)
    options = _clean_options(dict(body.options))
    if body.api_token:
        options["api_token_enc"] = secretbox.encrypt("panel", body.api_token)
    panel = Panel(
        shop_id=ctx.shop_id,
        name=body.name,
        kind=body.kind,
        base_url=url,
        username=body.username,
        password_enc=secretbox.encrypt("panel", body.password or ""),
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
    await _check_location(db, body, ctx)
    secrets_changing = bool(body.password or body.api_token) or url != panel.base_url
    if secrets_changing and not ctx.can("panels.secrets"):
        raise HTTPException(status_code=403, detail="secrets_need_owner")
    options = _clean_options(dict(body.options))
    if (panel.options or {}).get("api_token_enc"):
        options["api_token_enc"] = panel.options["api_token_enc"]
    if body.api_token:
        options["api_token_enc"] = secretbox.encrypt("panel", body.api_token)
    if not body.username.strip() and not (body.kind in TOKEN_PANELS and options.get("api_token_enc")):
        raise HTTPException(status_code=422, detail="username_required")
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
    except PanelError as exc:
        panel.is_healthy, panel.health_error, panel.health_checked_at = False, str(exc)[:255], utcnow()
        return {"ok": False, "error": str(exc)[:255], "status": exc.status}
    panel.is_healthy, panel.health_error, panel.health_checked_at = True, None, utcnow()
    # The connection works even when the panel admin may not list groups or inbounds; say why the
    # picker is empty instead of failing the whole test.
    options_error = None
    try:
        options = [{"id": o.id, "title": o.title} for o in await driver.list_options()]
    except PanelError as exc:
        options, options_error = [], str(exc)[:255]
    return {
        "ok": True,
        "detail": health.detail,
        "users": health.users,
        "version": health.version,
        "options": options,
        "options_error": options_error,
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
    if panel.kind not in {PanelKind.MARZBAN, PanelKind.PASARGUARD}:
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


# --- importing existing panel users (TEN-10) -------------------------------------------

PREVIEW_LIMIT = 500


@router.get("/panels/{panel_id}/import")
async def import_preview(panel_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    from paurubot.provisioning import importer

    panel = await owned(db, Panel, panel_id, ctx)
    try:
        users = await importer.untracked(db, panel)
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_unreachable") from exc
    ids = {u.username: importer.telegram_id_of(u) for u in users}
    known = set(
        await db.scalars(
            select(Customer.telegram_id).where(
                Customer.shop_id == ctx.shop_id, Customer.telegram_id.in_([i for i in ids.values() if i])
            )
        )
    )
    return {
        "total": len(users),
        "items": [
            {
                "username": u.username,
                "status": u.status,
                "used_bytes": u.used_bytes,
                "data_limit": u.data_limit,
                "expire_at": u.expire_at.isoformat() if u.expire_at else None,
                "telegram_id": ids[u.username],
                "known_customer": ids[u.username] in known,
            }
            for u in users[:PREVIEW_LIMIT]
        ],
    }


class ImportBody(BaseModel):
    usernames: list[str] | None = Field(default=None, max_length=50_000)
    customer_id: int | None = None
    match_telegram: bool = True


@router.post("/panels/{panel_id}/import")
async def import_users(panel_id: int, body: ImportBody, ctx: Writer, db: DB) -> dict[str, Any]:
    from paurubot.worker import queue

    panel = await owned(db, Panel, panel_id, ctx)
    if body.customer_id is not None:
        await owned(db, Customer, body.customer_id, ctx)
    elif not body.match_telegram:
        raise HTTPException(status_code=422, detail="import_needs_owner")
    job_id = await queue.enqueue(
        db,
        "ops",
        "import_panel",
        {
            "panel_id": panel.id,
            "usernames": body.usernames,
            "customer_id": body.customer_id,
            "match_telegram": body.match_telegram,
            "staff_id": ctx.staff.id,
        },
        shop_id=ctx.shop_id,
        max_attempts=3,
    )
    return {"job_id": job_id}


RESTART_PATHS = {
    PanelKind.MARZBAN: ("POST", "/api/core/restart"),
    PanelKind.PASARGUARD: ("POST", "/api/nodes/reconnect"),
}


@router.post("/panels/{panel_id}/restart")
async def restart_core(panel_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    """Restart the panel's Xray core (Marzban) or reconnect all nodes (PasarGuard) (ADM-07)."""
    panel = await owned(db, Panel, panel_id, ctx)
    target = RESTART_PATHS.get(PanelKind(panel.kind))
    if target is None:
        raise HTTPException(status_code=400, detail="restart_not_supported")
    driver = await registry.driver_for(panel)
    try:
        await driver.call(*target)  # type: ignore[attr-defined]
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_unreachable") from exc
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="panel.restart", entity="panel", entity_id=panel.id
    )
    return {"ok": True}


@router.post("/panels/{panel_id}/check")
async def check_now(panel_id: int, ctx: Reader, db: DB) -> dict[str, Any]:
    """Run one monitoring probe right away and return the fresh figures (PNL-12, PNL-13)."""
    from paurubot.provisioning import monitor

    panel = await owned(db, Panel, panel_id, ctx)
    result = await monitor.check_panel(db, panel)
    up = await monitor.uptime(db, [panel.id])
    return {
        **panel_out(panel),
        "uptime": up.get(panel.id),
        "check": {"ok": result.ok, "latency_ms": result.latency_ms, "error": result.error},
    }


# ---------------------------------------------------------------- moving users between panel admins (TEN-11)


@router.get("/panels/{panel_id}/admins")
async def panel_admins(panel_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    """The panel's admins and how many users each one owns."""
    from paurubot.panels import driver_for
    from paurubot.provisioning import importer

    panel = await owned(db, Panel, panel_id, ctx)
    try:
        driver = await driver_for(panel)
        names = await driver.list_admins()
        users = await importer.all_users(panel)
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="admins_unsupported") from exc
    owned_by: dict[str, int] = {}
    for user in users:
        if user.owner:
            owned_by[user.owner] = owned_by.get(user.owner, 0) + 1
    rows = sorted(set(names) | set(owned_by))
    return {"admins": [{"username": n, "users": owned_by.get(n, 0)} for n in rows], "total": len(users)}


class TransferBody(BaseModel):
    from_admin: str = Field(min_length=1, max_length=64)
    to_admin: str = Field(min_length=1, max_length=64)


@router.post("/panels/{panel_id}/transfer")
async def transfer_users(panel_id: int, body: TransferBody, ctx: Writer, db: DB) -> dict[str, Any]:
    """Give every user of one panel admin to another; one retrying job per user."""
    import uuid

    from paurubot.panels import driver_for
    from paurubot.provisioning import importer
    from paurubot.worker import queue

    panel = await owned(db, Panel, panel_id, ctx)
    if body.from_admin == body.to_admin:
        raise HTTPException(status_code=422, detail="same_admin")
    try:
        if body.to_admin not in await (await driver_for(panel)).list_admins():
            raise HTTPException(status_code=422, detail="unknown_admin")
        users = [u for u in await importer.all_users(panel) if u.owner == body.from_admin]
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="admins_unsupported") from exc
    batch = uuid.uuid4().hex[:12]
    for user in users:
        await queue.enqueue(
            db,
            "bulk",
            "set_owner",
            {
                "panel_id": panel.id,
                "username": user.username,
                "data": user.data or {},
                "admin": body.to_admin,
            },
            shop_id=ctx.shop_id,
            dedupe_key=f"owner:{batch}:{user.username}",
            max_attempts=6,
        )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel.transfer_users",
        entity="panel",
        entity_id=panel.id,
        after={"from": body.from_admin, "to": body.to_admin, "count": len(users), "batch": batch},
    )
    return {"queued": len(users)}
