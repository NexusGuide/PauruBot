"""Shop-branded subscription links: /s/<token> (SVC-08).

Client apps get the panel's subscription body with the usage headers they understand, so the
panel address never reaches customers. Browsers get a small page with usage, a QR code, the
link and one-tap import buttons for the common apps.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

from paurubot.config import get_settings
from paurubot.db.models import Service

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")

# User agents of VPN client apps; anything else that asks for HTML is treated as a browser.
CLIENT_UA = re.compile(
    r"v2ray|xray|hiddify|streisand|shadowrocket|sing-?box|clash|mihomo|stash|nekobox|nekoray|"
    r"foxray|v2box|karing|happ|quantumult|surge|loon|outline|npv|fair|exclave|husi|throne",
    re.IGNORECASE,
)

DEFAULT_APPS: list[dict[str, str]] = [
    {"name": "Hiddify", "url": "hiddify://import/{url}#{title}"},
    {"name": "v2rayNG", "url": "v2rayng://install-config?url={url}#{title}"},
    {"name": "Streisand", "url": "streisand://import/{url}#{title}"},
    {"name": "Happ", "url": "happ://add/{url}"},
]

ALLOWED_SCHEMES = re.compile(r"^[a-z][a-z0-9+.-]{1,24}://", re.IGNORECASE)
BLOCKED_SCHEMES = {"javascript", "data", "vbscript", "file", "blob"}


def branded_url(service: Service) -> str | None:
    base = get_settings().public_url
    if not base or not service.sub_token:
        return None
    return f"{base}/s/{service.sub_token}"


def customer_link(service: Service, cfg: dict[str, Any]) -> str | None:
    """The subscription link to show the customer: branded when enabled, else the panel's.

    WireGuard and login panels (PNL-08, PNL-11) have no link meant for customers: what the panel
    holds is an internal download address, so only the branded page is ever shown for them.
    """
    if service.panel is not None and service.panel.national_mode:
        return None
    from paurubot.panels.registry import delivery_of

    if delivery_of(service.panel.kind if service.panel is not None else None) != "subscription":
        return branded_url(service)
    if cfg.get("branded_sub_links"):
        branded = branded_url(service)
        if branded:
            return branded
    return service.subscription_url


def wants_page(user_agent: str, accept: str) -> bool:
    return "text/html" in accept and not CLIENT_UA.search(user_agent or "")


def validate_app_links(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("app_links must be a list of at most 12 apps")
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each app needs a name and a url")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or len(name) > 32 or not url or len(url) > 300:
            raise ValueError("each app needs a name (32 chars) and a url (300 chars)")
        scheme = url.split(":", 1)[0].lower()
        if not ALLOWED_SCHEMES.match(url) or scheme in BLOCKED_SCHEMES:
            raise ValueError("app url must start with a scheme like hiddify://")
        clean.append({"name": name, "url": url})
    return clean


def app_buttons(cfg: dict[str, Any], link: str, title: str) -> list[dict[str, str]]:
    apps = cfg.get("app_links") or DEFAULT_APPS
    out = []
    for app in apps:
        try:
            url = str(app["url"]).replace("{url}", quote(link, safe="")).replace("{title}", quote(title))
        except (KeyError, TypeError):
            continue
        scheme = url.split(":", 1)[0].lower()
        if scheme in BLOCKED_SCHEMES:
            continue
        out.append({"name": str(app.get("name") or ""), "url": url})
    return out


def profile_title(title: str) -> str:
    return "base64:" + base64.b64encode(title.encode()).decode()
