"""Serve the built frontends: /app (Mini App) and /admin (panel), with SPA fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


PUBLIC_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "application/javascript",
    # The Mini App's start script (REF-03): it loads Telegram's SDK and reads the launch data, so
    # it must arrive as a script; the app shell in its place is refused under nosniff.
    "launch.js": "application/javascript",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "favicon.png": "image/png",
}


def frontend_dir() -> Path:
    return Path(os.environ.get("FRONTEND_DIR", DEFAULT_DIR))


def mount_frontends(app: FastAPI) -> None:
    root = frontend_dir()
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def spa(name: str):  # type: ignore[no-untyped-def]
        base = (root / name).resolve()
        index = base / "index.html"

        async def serve(path: str = "") -> Response:
            # Real files next to index.html (manifest, service worker, icons) are served as files (APP-02).
            if path and "/" not in path and path in PUBLIC_FILES:
                candidate = (base / path).resolve()
                if candidate.parent == base and candidate.is_file():
                    return FileResponse(
                        candidate, media_type=PUBLIC_FILES[path], headers={"Cache-Control": "no-cache"}
                    )
            if not index.exists():
                return Response("frontend is not built", status_code=503, media_type="text/plain")
            return FileResponse(index, headers={"Cache-Control": "no-cache"})

        return serve

    def to_slash(name: str):  # type: ignore[no-untyped-def]
        # No parameters: a defaulted argument would become a query parameter (?name=//evil.com).
        async def redirect() -> RedirectResponse:
            return RedirectResponse(f"/{name}/")

        return redirect

    for name in ("app", "admin"):
        handler = spa(name)
        app.add_api_route(f"/{name}", to_slash(name), include_in_schema=False)
        app.add_api_route(f"/{name}/", handler, include_in_schema=False)
        app.add_api_route(f"/{name}/{{path:path}}", handler, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/admin/")
