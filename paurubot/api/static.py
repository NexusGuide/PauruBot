"""Serve the built frontends: /app (Mini App) and /admin (panel), with SPA fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def frontend_dir() -> Path:
    return Path(os.environ.get("FRONTEND_DIR", DEFAULT_DIR))


def mount_frontends(app: FastAPI) -> None:
    root = frontend_dir()
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def spa(name: str):  # type: ignore[no-untyped-def]
        index = root / name / "index.html"

        async def serve(path: str = "") -> Response:
            if not index.exists():
                return Response("frontend is not built", status_code=503, media_type="text/plain")
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

        return serve

    for name in ("app", "admin"):
        handler = spa(name)
        app.add_api_route(
            f"/{name}", lambda name=name: RedirectResponse(f"/{name}/"), include_in_schema=False
        )
        app.add_api_route(f"/{name}/", handler, include_in_schema=False)
        app.add_api_route(f"/{name}/{{path:path}}", handler, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/admin/")
