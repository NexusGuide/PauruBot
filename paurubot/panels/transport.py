"""Panel adapter for the shared validated-IP HTTP transport.

Preserve PanelError policy failures for existing driver callers.
"""

from __future__ import annotations

import httpx

from paurubot.panels.base import PanelError
from paurubot.security.http import UnsafeRequest
from paurubot.security.http import ValidatedTransport as OutboundTransport


class ValidatedTransport(OutboundTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except UnsafeRequest as exc:
            raise PanelError(str(exc)) from exc
