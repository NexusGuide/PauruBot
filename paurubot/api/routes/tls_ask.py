"""Caddy's question before an on-demand certificate (ClockBot parity B4): is this a shop's domain?

Caddy calls it inside the compose network; the public sites answer 404 for /internal/* paths
(deploy/Caddyfile). The answer is only yes or no, so a stray public call learns nothing useful.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from paurubot.api.deps import DB
from paurubot.core import domains

router = APIRouter(tags=["internal"])


@router.get("/internal/tls-ask", include_in_schema=False)
async def tls_ask(db: DB, domain: str = "") -> Response:
    allowed = await domains.tls_allowed(db, domain)
    return Response(status_code=200 if allowed else 404)
