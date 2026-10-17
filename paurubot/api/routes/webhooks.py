"""Panel webhooks (PNL-03): Marzban and PasarGuard push user events here."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from paurubot.api.body import BodyTooLarge, read_limited_body
from paurubot.api.deps import DB
from paurubot.db.models import Panel
from paurubot.panels import driver_for
from paurubot.panels.drivers.token_api import TokenApiDriver
from paurubot.provisioning.sync import apply_webhook_user
from paurubot.security.secretbox import decrypt
from paurubot.security.tokens import constant_equals

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

MAX_BODY = 512 * 1024
MAX_EVENTS = 500


@router.post("/panel/{panel_id}")
async def panel_webhook(panel_id: int, request: Request, db: DB) -> dict:
    panel = await db.get(Panel, panel_id)
    secret = decrypt("webhook", panel.webhook_secret_enc) if panel and panel.webhook_secret_enc else None
    if (
        panel is None
        or secret is None
        or not constant_equals(request.headers.get("x-webhook-secret"), secret)
    ):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        body = await read_limited_body(request, MAX_BODY)
    except BodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="too_large") from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    events = payload if isinstance(payload, list) else [payload]
    driver = await driver_for(panel)
    if not isinstance(driver, TokenApiDriver):
        raise HTTPException(status_code=404, detail="webhooks_not_supported")
    applied = 0
    for event in events[:MAX_EVENTS]:
        if not isinstance(event, dict):
            continue
        user = event.get("user")
        if not isinstance(user, dict) or not user.get("username"):
            continue
        if event.get("action") == "user_deleted":
            user = {**user, "status": "disabled"}
        if await apply_webhook_user(db, panel, driver.parse_user(user)) is not None:
            applied += 1
    return {"received": len(events), "applied": applied}
