"""Request-level security: correlation IDs, security headers, CSP, origin checks (SEC-05, SEC-06)."""

from __future__ import annotations

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
        except ClientDisconnect:
            # The client went away, or BodyLimitMiddleware cut an oversized body off: not our error.
            response = JSONResponse({"error": "client_disconnected", "correlation_id": cid}, status_code=400)
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


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count API requests by route template and status class (JOB-05)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        import time

        from paurubot.ops import metrics

        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/s/"):
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"
            if route.startswith("/s/{token}"):
                route = "/s/{token}"
            await metrics.count_http(
                request.method, route, response.status_code, time.perf_counter() - started
            )
        return response


# The largest request anything here needs: a receipt photo, a stock list or a usernames import.
MAX_BODY = 10 * 1024 * 1024
# The owner's import of another bot's database is the one big upload; it checks its own 300 MB cap.
LARGE_BODIES = {"/api/admin/migration/upload": 301 * 1024 * 1024}


def body_limit(path: str) -> int:
    return LARGE_BODIES.get(path.rstrip("/"), MAX_BODY)


class BodyLimitMiddleware:
    """Cap every request body before any route reads it, the anonymous ones included.

    FastAPI reads a JSON body into memory, and spools a multipart upload to disk, before the
    route's own checks or its sign-in run; without a cap one request could fill either.
    A declared Content-Length over the cap is refused at once; a body that streams past it is cut
    off there (the route sees a disconnect) and answered 413.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope.get("path", ""))
        declared = next((v for k, v in scope.get("headers", []) if k == b"content-length"), b"")
        if declared.isdigit() and (len(declared) > 12 or int(declared) > limit):
            await _too_large(send)
            return
        received = 0
        exceeded = started = False

        async def guarded_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # the route's answer to the cut-off body is replaced by ours
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await _too_large(send)


async def _too_large(send: Send) -> None:
    response = JSONResponse({"error": "too_large"}, status_code=413, headers={"Connection": "close"})
    await send(
        {"type": "http.response.start", "status": response.status_code, "headers": response.raw_headers}
    )
    await send({"type": "http.response.body", "body": response.body})
