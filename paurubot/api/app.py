from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from paurubot import __version__
from paurubot.api.middleware import CorrelationMiddleware, OriginCheckMiddleware, SecurityHeadersMiddleware
from paurubot.logs import correlation_id


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

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    from paurubot.api.routes import auth, setup

    app.include_router(setup.router)
    app.include_router(auth.router)
    from paurubot.api.routes import register_optional

    register_optional(app)
    return app
