"""Panel webhooks (PNL-03): Marzban, PasarGuard, Rebecca and Remnawave push user events here."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from paurubot.api.body import BodyTooLarge, read_limited_body
from paurubot.api.deps import DB
from paurubot.db.models import Panel, PanelKind
from paurubot.panels import PanelUser
from paurubot.provisioning.sync import apply_webhook_user
from paurubot.security.secretbox import decrypt
from paurubot.security.tokens import constant_equals

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

MAX_BODY = 512 * 1024
MAX_EVENTS = 500
TOKEN_WEBHOOK_KINDS = frozenset({PanelKind.MARZBAN, PanelKind.PASARGUARD, PanelKind.REBECCA})
SUPPORTED_WEBHOOK_KINDS = TOKEN_WEBHOOK_KINDS | frozenset({PanelKind.REMNAWAVE})


def _panel_kind(panel: Panel) -> PanelKind | None:
    raw = getattr(panel, "kind", None)
    if raw is None:
        return None
    try:
        return PanelKind(raw)
    except ValueError:
        return None


def _verify_remnawave_signature(body: bytes, secret: str, header: str | None) -> bool:
    if not header:
        return False
    provided = header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return constant_equals(provided.lower(), expected)


def _extract_username(event: dict, kind: PanelKind | None) -> str | None:
    if kind == PanelKind.REMNAWAVE:
        data = event.get("data")
        if isinstance(data, dict) and isinstance(data.get("username"), str):
            return data["username"]
    user = event.get("user")
    if isinstance(user, dict) and isinstance(user.get("username"), str):
        return user["username"]
    return None


@router.post("/panel/{panel_id}")
async def panel_webhook(panel_id: int, request: Request, db: DB) -> dict:
    panel = await db.get(Panel, panel_id)
    secret = decrypt("webhook", panel.webhook_secret_enc) if panel and panel.webhook_secret_enc else None
    if panel is None or secret is None:
        raise HTTPException(status_code=403, detail="forbidden")
    kind = _panel_kind(panel)
    if kind != PanelKind.REMNAWAVE and not constant_equals(request.headers.get("x-webhook-secret"), secret):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        body = await read_limited_body(request, MAX_BODY)
    except BodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="too_large") from exc
    if kind == PanelKind.REMNAWAVE and not _verify_remnawave_signature(
        body, secret, request.headers.get("x-remnawave-signature")
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    events = payload if isinstance(payload, list) else [payload]
    if kind is not None and kind not in SUPPORTED_WEBHOOK_KINDS:
        raise HTTPException(status_code=404, detail="webhooks_not_supported")
    if bool(getattr(panel, "national_mode", False)):
        return {"received": len(events), "applied": 0, "queued": 0}
    queued = 0
    seen: set[str] = set()
    for event in events[:MAX_EVENTS]:
        if not isinstance(event, dict):
            continue
        username = _extract_username(event, kind)
        if not isinstance(username, str) or not username or len(username) > 128 or username in seen:
            continue
        seen.add(username)
        # Neither event action nor its status/traffic fields are authoritative.
        if await apply_webhook_user(db, panel, PanelUser(username)) is not None:
            queued += 1
    return {"received": len(events), "applied": 0, "queued": queued}
