"""Request-level security: correlation IDs, security headers, CSP, origin checks (SEC-05, SEC-06)."""

from __future__ import annotations

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from paurubot.config import get_settings
from paurubot.logs import correlation_id

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TELEGRAM_FRAME_ANCESTORS = "https://web.telegram.org https://*.telegram.org"


def content_security_policy(path: str) -> str:
    """Strict CSP with no inline scripts or styles anywhere; the Mini App may be framed by Telegram."""
    is_webapp = path == "/app" or path.startswith("/app/")
    script_src = "'self' https://telegram.org" if is_webapp else "'self'"
    frame_ancestors = TELEGRAM_FRAME_ANCESTORS if is_webapp else "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self'",
            "img-src 'self' data: blob: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        ]
    )


class CorrelationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        cid = (
            incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() else uuid.uuid4().hex
        )
        token = correlation_id.set(cid)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error on %s %s", request.method, request.url.path)
            response = JSONResponse({"error": "internal_error", "correlation_id": cid}, status_code=500)
        finally:
            correlation_id.reset(token)
        response.headers["X-Request-ID"] = cid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        response.headers["Content-Security-Policy"] = content_security_policy(path)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not (path == "/app" or path.startswith("/app/")):
            response.headers["X-Frame-Options"] = "DENY"
        if get_settings().is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Reject cross-site state-changing requests to cookie-authenticated admin endpoints."""

    protected_prefixes = ("/api/admin/", "/api/auth/", "/api/setup/")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in SAFE_METHODS and request.url.path.startswith(self.protected_prefixes):
            if not origin_allowed(request):
                return JSONResponse({"error": "bad_origin"}, status_code=403)
        return await call_next(request)


def origin_allowed(request: Request) -> bool:
    allowed = set(get_settings().origins)
    own = f"{request.url.scheme}://{request.url.netloc}"
    allowed.add(own)
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/") in allowed
    referer = request.headers.get("referer")
    if referer:
        return any(referer == base or referer.startswith(base + "/") for base in allowed)
    # Non-browser clients (CLI, tests) send neither header; CSRF tokens still protect them.
    return True
