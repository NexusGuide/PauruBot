from __future__ import annotations

import importlib

from fastapi import FastAPI

# Routers added phase by phase; each module exposes `router`.
OPTIONAL_ROUTERS: list[str] = ["pay", "webhooks", "telegram", "webapp"]


def register_optional(app: FastAPI) -> None:
    for module_name in OPTIONAL_ROUTERS:
        module = importlib.import_module(f"paurubot.api.routes.{module_name}")
        app.include_router(module.router)
