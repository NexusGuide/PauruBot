"""In-memory fakes of the three panel APIs, served through httpx.MockTransport."""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx


class FakeTokenPanel:
    """Marzban / PasarGuard behaviour shared by both APIs."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.users: dict[str, dict[str, Any]] = {}
        self.token = "tok-" + secrets.token_hex(4)
        self.logins = 0
        self.fail_next: int | None = None
        self.calls: list[tuple[str, str]] = []
        self.webhook_payloads: list[Any] = []
        self.sub_agents: list[str] = []
        self.nodes: list[dict[str, Any]] | None = None
        self.admin: dict[str, Any] | None = None
        self.admins: set[str] = set()

    def _user_out(self, user: dict[str, Any]) -> dict[str, Any]:
        out = dict(user)
        out["subscription_url"] = f"/sub/{user['sub']}"
        owner = user.get("admin") or "admin"
        out["admin"] = {"username": owner} if self.kind == "pasarguard" else owner
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path.startswith("/sub/"):
            user = next((u for u in self.users.values() if f"/sub/{u['sub']}" == path), None)
            if user is None:
                return httpx.Response(404, text="no such subscription")
            self.sub_agents.append(request.headers.get("user-agent", ""))
            body = base64.b64encode(f"vless://{user['username']}@example.com:443#{user['username']}".encode())
            return httpx.Response(
                200,
                content=body,
                headers={
                    "content-type": "text/plain; charset=utf-8",
                    "subscription-userinfo": f"upload=0; download={user['used_traffic']}; total={user['data_limit']}",
                    "x-panel-secret": "must-not-leak",
                },
            )
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"detail": "boom"})
        if path == "/api/admin/token":
            form = parse_qs(request.content.decode())
            if form.get("password") != ["panel-password"]:
                return httpx.Response(401, json={"detail": "bad creds"})
            self.logins += 1
            return httpx.Response(200, json={"access_token": self.token})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if path == "/api/system":
            return httpx.Response(
                200,
                json={
                    "version": "1.0",
                    "total_user": len(self.users),
                    "cpu_usage": 12.5,
                    "cpu_cores": 4,
                    "mem_used": 2 * 1024**3,
                    "mem_total": 8 * 1024**3,
                    "incoming_bandwidth": 1000,
                    "outgoing_bandwidth": 2000,
                },
            )
        if path == "/api/nodes" and self.nodes is not None:
            return httpx.Response(200, json=self.nodes)
        if path == "/api/admin" and self.admin is not None:
            return httpx.Response(200, json=self.admin)
        if path == "/api/admins" and request.method == "GET":
            names = sorted({"admin", *self.admins, *(u.get("admin") or "admin" for u in self.users.values())})
            return httpx.Response(200, json=[{"username": n, "is_sudo": n == "admin"} for n in names])
        if path == "/api/users" and request.method == "GET":
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            rows = [self._user_out(u) for u in self.users.values()]
            return httpx.Response(200, json={"users": rows[offset : offset + limit], "total": len(rows)})
        if path == "/api/groups":
            return httpx.Response(200, json={"groups": [{"id": 1, "name": "default"}]})
        if path == "/api/inbounds":
            return httpx.Response(200, json={"vless": [{"tag": "VLESS TCP"}]})
        if path == "/api/user" and request.method == "POST":
            body = json.loads(request.content)
            name = body["username"]
            if name in self.users:
                return httpx.Response(409, json={"detail": "exists"})
            user = {
                "id": len(self.users) + 1,
                "username": name,
                "status": body.get("status", "active"),
                "used_traffic": 0,
                "data_limit": body.get("data_limit") or 0,
                "expire": body.get("expire") or 0,
                "sub": secrets.token_hex(6),
                "request": body,
            }
            self.users[name] = user
            return httpx.Response(200, json=self._user_out(user))
        prefix = "/api/user/by-username/" if self.kind == "pasarguard" else "/api/user/"
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            name, _, action = rest.partition("/")
            user = self.users.get(name)
            if user is None:
                return httpx.Response(404, json={"detail": "User not found"})
            if request.method == "GET" and not action:
                return httpx.Response(200, json=self._user_out(user))
            if request.method == "PUT" and not action:
                body = json.loads(request.content)
                user.update({k: v for k, v in body.items() if k in {"expire", "data_limit", "status"}})
                return httpx.Response(200, json=self._user_out(user))
            if request.method == "DELETE" and not action:
                del self.users[name]
                return httpx.Response(200, json={})
            if action == "reset":
                user["used_traffic"] = 0
                return httpx.Response(200, json=self._user_out(user))
            if action == "revoke_sub":
                user["sub"] = secrets.token_hex(6)
                return httpx.Response(200, json=self._user_out(user))
            if request.method == "PUT" and action in {"set-owner", "set_owner"}:
                expected = "set_owner" if self.kind == "pasarguard" else "set-owner"
                target = request.url.params.get("admin_username")
                if action != expected or not target:
                    return httpx.Response(404, json={"detail": "Not Found"})
                if target not in self.admins and target != "admin":
                    return httpx.Response(404, json={"detail": "Admin not found"})
                user["admin"] = target
                return httpx.Response(200, json=self._user_out(user))
        return httpx.Response(404, json={"detail": "Not Found"})


class FakeXuiPanel:
    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.traffic: dict[str, dict[str, int]] = {}
        self.logged_in = False
        self.fail_next: int | None = None

    def _ok(self, obj: Any = None) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "msg": "", "obj": obj})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, text="error")
        if path == "/login":
            form = parse_qs(request.content.decode())
            ok = form.get("password") == ["panel-password"]
            self.logged_in = ok
            return httpx.Response(
                200,
                json={"success": ok, "msg": ""},
                headers={"set-cookie": "3x-ui=session; Path=/"} if ok else {},
            )
        if not self.logged_in or "3x-ui=session" not in request.headers.get("cookie", ""):
            return httpx.Response(302, headers={"location": "/"})
        if path == "/panel/api/inbounds/list":
            return self._ok(
                [
                    {
                        "id": 3,
                        "remark": "main",
                        "protocol": "vless",
                        "port": 443,
                        "settings": json.dumps({"clients": list(self.clients.values())}),
                        "clientStats": [
                            {
                                "email": c["email"],
                                **self.traffic.get(c["email"], {}),
                                "enable": c.get("enable", True),
                            }
                            for c in self.clients.values()
                        ],
                    }
                ]
            )
        if path == "/panel/api/inbounds/get/3":
            return self._ok({"id": 3, "protocol": "vless"})
        form = parse_qs(request.content.decode()) if request.content else {}
        if path == "/panel/api/inbounds/addClient":
            client = json.loads(form["settings"][0])["clients"][0]
            self.clients[client["id"]] = client
            self.traffic[client["email"]] = {"up": 0, "down": 0}
            return self._ok()
        if path.startswith("/panel/api/inbounds/updateClient/"):
            key = path.rsplit("/", 1)[1]
            if key not in self.clients:
                return httpx.Response(200, json={"success": False, "msg": "Client Not Found"})
            client = json.loads(form["settings"][0])["clients"][0]
            del self.clients[key]
            self.clients[client["id"]] = client
            return self._ok()
        if path.startswith("/panel/api/inbounds/getClientTraffics/"):
            email = path.rsplit("/", 1)[1]
            client = next((c for c in self.clients.values() if c["email"] == email), None)
            if client is None:
                return self._ok(None)
            t = self.traffic[email]
            return self._ok(
                {
                    "email": email,
                    "up": t["up"],
                    "down": t["down"],
                    "total": client["totalGB"],
                    "expiryTime": client["expiryTime"],
                    "enable": client["enable"],
                }
            )
        if "/resetClientTraffic/" in path:
            email = path.rsplit("/", 1)[1]
            self.traffic[email] = {"up": 0, "down": 0}
            return self._ok()
        if "/delClient/" in path:
            key = path.rsplit("/", 1)[1]
            if key not in self.clients:
                return httpx.Response(200, json={"success": False, "msg": "Client not found"})
            email = self.clients.pop(key)["email"]
            self.traffic.pop(email, None)
            return self._ok()
        return httpx.Response(404, text="not found")


class FakeMarzneshinPanel:
    """Marzneshin: /api/admins/token, /api/users/{name}, enable/disable endpoints, expire strategies."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.token = "mz-" + secrets.token_hex(4)
        self.requests: list[tuple[str, str, Any]] = []

    def _out(self, user: dict[str, Any]) -> dict[str, Any]:
        out = dict(user)
        out["subscription_url"] = f"/sub/{user['username']}/{user['key']}"
        out["expired"] = bool(
            user["expire_strategy"] == "fixed_date"
            and user["expire_date"]
            and datetime.fromisoformat(user["expire_date"].replace("Z", "+00:00")) <= now()
        )
        out["data_limit_reached"] = bool(user["data_limit"] and user["used_traffic"] >= user["data_limit"])
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = (
            json.loads(request.content)
            if request.content and request.headers.get("content-type", "").startswith("application/json")
            else None
        )
        self.requests.append((request.method, path, body))
        if path == "/api/admins/token":
            form = parse_qs(request.content.decode())
            if form.get("password") != ["panel-password"]:
                return httpx.Response(401, json={"detail": "bad creds"})
            return httpx.Response(200, json={"access_token": self.token})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if path == "/api/system/stats/users":
            return httpx.Response(200, json={"total": len(self.users), "active": len(self.users)})
        if path == "/api/services":
            return httpx.Response(200, json={"items": [{"id": 1, "name": "main"}, {"id": 2, "name": "game"}]})
        if path == "/api/users" and request.method == "POST":
            assert body is not None
            if body["username"] in self.users:
                return httpx.Response(409, json={"detail": "exists"})
            user = {
                "id": len(self.users) + 1,
                "username": body["username"],
                "service_ids": body.get("service_ids") or [],
                "data_limit": body.get("data_limit") or 0,
                "used_traffic": 0,
                "expire_strategy": body.get("expire_strategy", "never"),
                "expire_date": body.get("expire_date"),
                "usage_duration": body.get("usage_duration"),
                "enabled": True,
                "activated": False,
                "note": body.get("note"),
                "key": secrets.token_hex(4),
            }
            self.users[user["username"]] = user
            return httpx.Response(200, json=self._out(user))
        if path == "/api/users" and request.method == "GET":
            page = int(request.url.params.get("page", 1))
            size = int(request.url.params.get("size", 50))
            rows = [self._out(u) for u in self.users.values()]
            return httpx.Response(
                200, json={"items": rows[(page - 1) * size : page * size], "total": len(rows)}
            )
        if path.startswith("/api/users/"):
            name, _, action = path[len("/api/users/") :].partition("/")
            user = self.users.get(name)
            if user is None:
                return httpx.Response(404, json={"detail": "User not found"})
            if request.method == "GET" and not action:
                return httpx.Response(200, json=self._out(user))
            if request.method == "PUT" and not action:
                assert body is not None
                user.update(
                    {k: v for k, v in body.items() if k in {"data_limit", "expire_strategy", "expire_date"}}
                )
                return httpx.Response(200, json=self._out(user))
            if request.method == "DELETE":
                del self.users[name]
                return httpx.Response(200, json={})
            if action in {"enable", "disable"}:
                user["enabled"] = action == "enable"
                return httpx.Response(200, json=self._out(user))
            if action == "reset":
                user["used_traffic"] = 0
                return httpx.Response(200, json=self._out(user))
            if action == "revoke_sub":
                user["key"] = secrets.token_hex(4)
                return httpx.Response(200, json=self._out(user))
        return httpx.Response(404, json={"detail": "Not Found"})


def transport(fake: FakeTokenPanel | FakeXuiPanel | FakeMarzneshinPanel) -> httpx.MockTransport:
    return httpx.MockTransport(fake.handler)


def now() -> datetime:
    return datetime.now(UTC)
