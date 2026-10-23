from __future__ import annotations

import importlib

from fastapi import FastAPI

# Routers added phase by phase; each module exposes `router`.
OPTIONAL_ROUTERS: list[str] = [
    "pay",
    "webhooks",
    "telegram",
    "webapp",
    "tonpay_custom",
    "announcements",
    "webapp_staff",
    "sub",
    "guest",
    "admin.dashboard",
    "admin.customers",
    "admin.commerce",
    "admin.catalog",
    "admin.manual_sales",
    "webapp_stock",
    "admin.panels",
    "admin.panel_capabilities",
    "admin.growth",
    "admin.people",
    "admin.system",
    "admin.resale",
    "admin.accounts",
    "admin.desk",
]


def register_optional(app: FastAPI) -> None:
    for module_name in OPTIONAL_ROUTERS:
        module = importlib.import_module(f"paurubot.api.routes.{module_name}")
        app.include_router(module.router)
