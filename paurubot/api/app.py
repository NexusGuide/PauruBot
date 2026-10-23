from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from paurubot import __version__
from paurubot.api.middleware import (
    CorrelationMiddleware,
    MetricsMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
)
from paurubot.logs import correlation_id

PRIVATE_NETWORKS = [
    ipaddress.ip_network(net)
    for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    from paurubot.db.session import dispose_engine
    from paurubot.kv import close_redis
    from paurubot.panels import close_all

    await close_all()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="PauruBot",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    # Starlette runs middlewares in reverse order of registration: correlation is outermost.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationMiddleware)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": exc.detail, "correlation_id": correlation_id.get()},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(p) for p in err.get("loc", [])[1:]) for err in exc.errors()]
        return JSONResponse(
            {"error": "validation_error", "fields": fields, "correlation_id": correlation_id.get()},
            status_code=422,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        """Prometheus scrape target (JOB-05): a bearer token, or a private address when none is set."""
        from paurubot.api.deps import request_ip
        from paurubot.config import get_settings
        from paurubot.db.session import session_scope
        from paurubot.ops import metrics
        from paurubot.security.tokens import constant_equals

        token = get_settings().metrics_token.get_secret_value()
        if token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not constant_equals(given, token):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        else:
            try:
                address = ipaddress.ip_address(request_ip(request))
            except ValueError:
                address = None
            private = address is not None and any(address in net for net in PRIVATE_NETWORKS)
            # A request relayed by the reverse proxy came from outside even when the proxy's own
            # private address is all we see (TRUST_PROXY_HOPS=0): never serve it without a token.
            proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
            if not private or proxied:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        async with session_scope() as session:
            body = await metrics.render(session)
        return Response(body, media_type="text/plain; version=0.0.4")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        import asyncio

        from alembic.script import ScriptDirectory
        from sqlalchemy import text

        from paurubot.db.migrate import alembic_config
        from paurubot.db.session import get_sessionmaker
        from paurubot.kv import get_redis

        try:
            async with asyncio.timeout(3):
                async with get_sessionmaker()() as session:
                    await session.execute(text("SELECT 1"))
                    # Require the image's migration heads, not merely a reachable DB.
                    revisions = await session.scalars(text("SELECT version_num FROM alembic_version"))
                    expected = set(ScriptDirectory.from_config(alembic_config()).get_heads())
                    if not expected or set(revisions.all()) != expected:
                        return JSONResponse({"status": "not_ready"}, status_code=503)
                await get_redis().ping()
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready", "version": __version__})

    from paurubot.api.routes import auth, setup

    app.include_router(setup.router)
    app.include_router(auth.router)
    from paurubot.api.routes import register_optional

    register_optional(app)
    from paurubot.api.static import mount_frontends

    mount_frontends(app)
    return app
