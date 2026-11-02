from __future__ import annotations

from typing import Any

from paurubot.panels.base import PanelOption, PanelUser, UserSpec
from paurubot.panels.drivers.token_api import TokenApiDriver


class MarzbanDriver(TokenApiDriver):
    """Marzban: users carry `proxies` and an optional per-protocol `inbounds` selection (PNL-04)."""

    kind = "marzban"
    # /sub/{token}/{client_type} (F-32); Rebecca keeps Marzban's paths.
    sub_formats = {
        "v2ray": "v2ray",
        "clash": "clash",
        "clash-meta": "clash-meta",
        "sing-box": "sing-box",
        "xray-json": "v2ray-json",
    }

    async def list_options(self) -> list[PanelOption]:
        data = await self.call("GET", "/api/inbounds")
        options: list[PanelOption] = []
        for protocol, inbounds in (data or {}).items():
            for inbound in inbounds or []:
                tag = inbound.get("tag")
                if tag:
                    options.append(PanelOption(id=f"{protocol}:{tag}", title=f"{protocol} · {tag}"))
        return options

    def proxies_and_inbounds(
        self, overrides: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, list[str]] | None]:
        opts = {**self.options, **(overrides or {})}
        selected: list[str] = [str(item) for item in opts.get("inbounds") or []]
        flow = opts.get("flow")
        if not selected:
            default_proxies: dict[str, Any] = dict(opts.get("proxies") or {"vless": {}})
            if flow and "vless" in default_proxies:
                default_proxies["vless"] = {"flow": flow}
            return default_proxies, None
        inbounds: dict[str, list[str]] = {}
        for item in selected:
            protocol, _, tag = item.partition(":")
            inbounds.setdefault(protocol, []).append(tag)
        proxies: dict[str, Any] = {protocol: {} for protocol in inbounds}
        if flow and "vless" in proxies:
            proxies["vless"] = {"flow": flow}
        return proxies, inbounds

    async def create_user(self, spec: UserSpec) -> PanelUser:
        proxies, inbounds = self.proxies_and_inbounds(spec.options)
        payload: dict[str, Any] = {
            "username": spec.username,
            "proxies": proxies,
            "data_limit": spec.data_limit,
            "data_limit_reset_strategy": "no_reset",
            "note": spec.note,
            **self.lifecycle_fields(spec),
        }
        if inbounds:
            payload["inbounds"] = inbounds
        data = await self.call("POST", "/api/user", json=payload)
        return self.parse_user(data)
