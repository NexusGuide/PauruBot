"""Updates and rollbacks requested from the admin panel (OPS-07).

The app never runs Docker itself. It leaves one request in Redis and the host agent
(`pauru enable-agent`, a systemd service) carries it out with the installer's own update
and rollback actions, then reports progress back under the same `updater:` keys.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from paurubot import __version__, kv

REQUEST_KEY = "updater:request"
REQUEST_TTL = 300
AGENT_STALE_SECONDS = 60
CHANNELS = ("stable", "beta")
VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")
STATE_KEYS = ("heartbeat", "version", "channel", "previous", "latest", "state", "last_action", "log")


class UpdateError(Exception):
    """A request the agent would refuse; the message is the API error code."""


@dataclass(slots=True)
class Status:
    agent_online: bool
    app_version: str
    installed: str | None
    channel: str | None
    previous: str | None
    latest: str | None
    state: str | None
    last_action: str | None
    log: str | None
    pending: dict[str, str] | None
    update_available: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _newer(latest: str | None, installed: str | None) -> bool:
    def parts(tag: str) -> tuple[int, ...]:
        core = tag.lstrip("v").split("-", 1)[0]
        return tuple(int(p) for p in core.split(".") if p.isdigit())

    if not latest or not installed or not VERSION_RE.match(latest) or not VERSION_RE.match(installed):
        return False
    return parts(latest) > parts(installed)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def status() -> Status:
    redis = kv.get_redis()
    values = await redis.mget([f"updater:{k}" for k in STATE_KEYS])
    data: dict[str, str | None] = {k: _text(v) for k, v in zip(STATE_KEYS, values, strict=True)}
    pending_raw = _text(await redis.get(REQUEST_KEY))
    pending = None
    if pending_raw:
        try:
            pending = {k: str(v) for k, v in json.loads(pending_raw).items()}
        except (ValueError, AttributeError):
            pending = None
    try:
        beat = int(data["heartbeat"] or 0)
    except ValueError:
        beat = 0
    state = data["state"]
    return Status(
        agent_online=time.time() - beat < AGENT_STALE_SECONDS,
        app_version=__version__,
        installed=data["version"] or None,
        channel=data["channel"] or None,
        previous=data["previous"] or None,
        latest=data["latest"] or None,
        state=state,
        last_action=data["last_action"],
        log=data["log"],
        pending=pending,
        update_available=_newer(data["latest"], data["version"]),
    )


def build_request(action: str, channel: str | None, version: str | None) -> dict[str, str]:
    if action not in ("update", "rollback"):
        raise UpdateError("bad_action")
    if channel and channel not in CHANNELS:
        raise UpdateError("bad_channel")
    if version and not VERSION_RE.match(version):
        raise UpdateError("bad_version")
    request = {"action": action}
    if action == "update":
        if channel:
            request["channel"] = channel
        if version:
            request["version"] = version
    return request


async def request(action: str, channel: str | None = None, version: str | None = None) -> dict[str, str]:
    """Queue one action for the agent. Only one may wait or run at a time."""
    body = build_request(action, channel, version)
    current = await status()
    if not current.agent_online:
        raise UpdateError("agent_offline")
    if current.state == "running":
        raise UpdateError("update_running")
    if action == "rollback" and not current.previous:
        raise UpdateError("nothing_to_roll_back")
    # The agent reads fields with a plain pattern, so keep the exact `"key": "value"` layout.
    payload = json.dumps(body, separators=(", ", ": "))
    if not await kv.get_redis().set(REQUEST_KEY, payload, nx=True, ex=REQUEST_TTL):
        raise UpdateError("update_pending")
    return body


async def cancel() -> bool:
    return bool(await kv.get_redis().delete(REQUEST_KEY))
