"""In-memory fakes of the three panel APIs, served through httpx.MockTransport."""

from __future__ import annotations

import base64
import json
import re
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
        self.sub_formats: list[str | None] = []
        self.nodes: list[dict[str, Any]] | None = None
        self.admin: dict[str, Any] | None = None
        self.admins: set[str] = set()
        self.groups: list[dict[str, Any]] = [{"id": 1, "name": "default"}]
        # PasarGuard role permissions for groups: "read" (full list), "read_simple" or none.
        self.group_perms: set[str] = {"read", "read_simple"}
        # PasarGuard admins the bot sold (TEN-14), and what was done to their users.
        self.sold_admins: dict[str, dict[str, Any]] = {}
        self.admin_user_actions: list[tuple[str, str]] = []
        self.roles: list[dict[str, Any]] = [{"id": 2, "name": "reseller"}, {"id": 3, "name": "limited"}]

    def _user_out(self, user: dict[str, Any]) -> dict[str, Any]:
        out = dict(user)
        out["subscription_url"] = f"/sub/{user['sub']}"
        owner = user.get("admin") or "admin"
        out["admin"] = {"username": owner} if self.kind == "pasarguard" else owner
        return out

    def _sold_admin(self, request: httpx.Request, path: str) -> httpx.Response | None:
        """PasarGuard's admin API, as far as selling admin accounts uses it."""
        if path == "/api/admin-roles/simple":
            return httpx.Response(200, json={"roles": self.roles, "total": len(self.roles)})
        if path == "/api/admins" and request.method == "GET" and request.url.params.get("usernames"):
            wanted = set(request.url.params.get_list("usernames"))
            rows = [a for n, a in self.sold_admins.items() if n in wanted]
            return httpx.Response(200, json={"admins": rows, "total": len(rows)})
        if path == "/api/admin" and request.method == "POST":
            body = json.loads(request.content)
            password = body.get("password", "")
            strong = (
                len(password) >= 12
                and sum(c.isupper() for c in password) >= 2
                and sum(c.islower() for c in password) >= 2
                and sum(c.isdigit() for c in password) >= 2
                and any(not c.isalnum() for c in password)
                and '"' not in password
            )
            if not strong:
                return httpx.Response(422, json={"detail": [{"msg": "Value error, weak password"}]})
            if body["username"] in self.sold_admins:
                return httpx.Response(409, json={"detail": "Admin already exists"})
            if body.get("role_id") not in {r["id"] for r in self.roles}:
                return httpx.Response(404, json={"detail": "Role not found"})
            self.sold_admins[body["username"]] = {
                **body,
                "status": "active",
                "used_traffic": 0,
                "total_users": 0,
            }
            return httpx.Response(200, json=self.sold_admins[body["username"]])
        match = re.fullmatch(r"/api/admin/([^/]+)(/.*)?", path)
        if match is None or match.group(1) in {"token"}:
            return None
        name, action = match.group(1), match.group(2) or ""
        admin = self.sold_admins.get(name)
        if admin is None:
            return httpx.Response(404, json={"detail": "Admin not found"})
        if request.method == "PUT" and not action:
            admin.update(json.loads(request.content))
            return httpx.Response(200, json=admin)
        if request.method == "DELETE" and not action:
            del self.sold_admins[name]
            return httpx.Response(200, json={})
        if request.method == "POST" and action == "/reset":
            admin["used_traffic"] = 0
            return httpx.Response(200, json=admin)
        if request.method == "POST" and action in {"/users/activate", "/users/disable"}:
            self.admin_user_actions.append((name, action.rsplit("/", 1)[1]))
            return httpx.Response(200, json={})
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path.startswith("/sub/"):
            # /sub/{token} or, for a named client format, /sub/{token}/{client_type} (F-32)
            parts = path.split("/")
            fmt = parts[3] if len(parts) == 4 else None
            base = "/".join(parts[:3])
            user = next((u for u in self.users.values() if f"/sub/{u['sub']}" == base), None)
            if user is None or len(parts) > 4:
                return httpx.Response(404, text="no such subscription")
            self.sub_agents.append(request.headers.get("user-agent", ""))
            self.sub_formats.append(fmt)
            if fmt is not None:
                return httpx.Response(200, content=f"format:{fmt}:{user['username']}".encode())
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
        if self.kind == "pasarguard":
            answer = self._sold_admin(request, path)
            if answer is not None:
                return answer
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
            if "read" not in self.group_perms:
                return httpx.Response(403, json={"detail": "Permission denied: groups.read"})
            return httpx.Response(200, json={"groups": self.groups, "total": len(self.groups)})
        if path == "/api/groups/simple":
            if "read_simple" not in self.group_perms:
                return httpx.Response(403, json={"detail": "Permission denied: groups.read_simple"})
            simple = [{"id": g["id"], "name": g["name"]} for g in self.groups]
            return httpx.Response(200, json={"groups": simple, "total": len(simple)})
        if path == "/api/inbounds":
            return httpx.Response(200, json={"vless": [{"tag": "VLESS TCP"}]})
        if path == "/api/user" and request.method == "POST":
            body = json.loads(request.content)
            name = body["username"]
            if self.kind == "pasarguard":  # PasarGuard's own UserCreate validation
                problems = []
                if not body.get("group_ids"):
                    problems.append("Value error, you must select at least one group")
                if re.search(r"[-_@.]{2,}", name):
                    problems.append("Value error, Username cannot have consecutive special characters")
                if problems:
                    return httpx.Response(
                        422, json={"detail": [{"loc": ["body"], "msg": m} for m in problems]}
                    )
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
        self.ips: dict[str, list[str]] = {}

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
        if path.startswith("/panel/api/inbounds/clientIps/"):
            seen = self.ips.get(path.rsplit("/", 1)[1])
            return self._ok([f"{ip} (2026-10-07 10:00:00)" for ip in seen] if seen else "No IP Record")
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


class FakeRemnawavePanel:
    """Remnawave: {"response": ...} envelopes, users by UUID, actions under /actions, squads."""

    def __init__(self, api_token: str = "rw-token") -> None:
        self.api_token = api_token
        self.users: dict[str, dict[str, Any]] = {}  # uuid -> user
        self.fail_next: int | None = None
        self.forwarded: list[str | None] = []
        self.logins = 0

    @property
    def token(self) -> str:
        return self.api_token

    @token.setter
    def token(self, value: str) -> None:
        self.api_token = value

    @staticmethod
    def _ok(obj: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"response": obj})

    def _out(self, u: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in u.items() if k != "used"}
        out["userTraffic"] = {"usedTrafficBytes": u["used"], "onlineAt": None}
        out["subscriptionUrl"] = f"https://sub.example.com/{u['shortUuid']}"
        return out

    def _by_name(self, name: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if u["username"] == name), None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.forwarded.append(request.headers.get("x-forwarded-proto"))
        if self.fail_next:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"message": "boom"})
        if path == "/api/auth/login":
            body = json.loads(request.content)
            if body.get("password") != "panel-password":
                return httpx.Response(401, json={"message": "bad"})
            self.logins += 1
            return self._ok({"accessToken": self.api_token}, 201)
        if request.headers.get("authorization") != f"Bearer {self.api_token}":
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/api/system/stats":
            return self._ok(
                {
                    "cpu": {"cores": 4},
                    "memory": {"total": 8 * 1024**3, "used": 2 * 1024**3, "free": 6 * 1024**3},
                    "users": {"totalUsers": len(self.users), "statusCounts": {"ACTIVE": len(self.users)}},
                    "onlineStats": {"onlineNow": 1, "lastDay": 2, "lastWeek": 3, "neverOnline": 0},
                    "nodes": {"totalOnline": 1, "totalBytesLifetime": "0"},
                }
            )
        if path == "/api/nodes":
            return self._ok([{"uuid": "n1", "name": "DE", "isConnected": True, "isDisabled": False}])
        if path == "/api/internal-squads":
            return self._ok({"total": 1, "internalSquads": [{"uuid": "sq-1", "name": "Default"}]})
        if path == "/api/users" and request.method == "GET":
            start = int(request.url.params.get("start", 0))
            size = int(request.url.params.get("size", 25))
            rows = [self._out(u) for u in self.users.values()]
            return self._ok({"users": rows[start : start + size], "total": len(rows)})
        if path == "/api/users" and request.method == "POST":
            body = json.loads(request.content)
            if self._by_name(body["username"]):
                return httpx.Response(400, json={"message": "User username already exists"})
            uid = f"uuid-{len(self.users) + 1}"
            user = {
                "uuid": uid,
                "id": len(self.users) + 1,
                "shortUuid": secrets.token_hex(4),
                "username": body["username"],
                "status": body.get("status", "ACTIVE"),
                "trafficLimitBytes": body.get("trafficLimitBytes", 0),
                "expireAt": body["expireAt"],
                "description": body.get("description"),
                "activeInternalSquads": [
                    {"uuid": s, "name": s} for s in body.get("activeInternalSquads", [])
                ],
                "telegramId": body.get("telegramId"),
                "used": 0,
            }
            self.users[uid] = user
            return self._ok(self._out(user), 201)
        if path == "/api/users" and request.method == "PATCH":
            body = json.loads(request.content)
            user = self.users.get(body.get("uuid", ""))
            if user is None:
                return httpx.Response(404, json={"message": "not found"})
            for key in ("expireAt", "trafficLimitBytes", "status", "tag"):
                if key in body:
                    user[key] = body[key]
            self.patches = getattr(self, "patches", 0) + 1
            return self._ok(self._out(user))
        if path.startswith("/api/users/by-username/"):
            user = self._by_name(path.rsplit("/", 1)[1])
            return self._ok(self._out(user)) if user else httpx.Response(404, json={"message": "not found"})
        if path.startswith("/api/users/"):
            rest = path[len("/api/users/") :]
            uid, _, action = rest.partition("/")
            user = self.users.get(uid)
            if user is None:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "GET" and not action:
                return self._ok(self._out(user))
            if request.method == "DELETE" and not action:
                del self.users[uid]
                return self._ok({"isDeleted": True})
            if action == "actions/enable":
                user["status"] = "ACTIVE"
            elif action == "actions/disable":
                user["status"] = "DISABLED"
            elif action == "actions/reset-traffic":
                user["used"] = 0
            elif action == "actions/revoke":
                user["shortUuid"] = secrets.token_hex(4)
            else:
                return httpx.Response(404, json={"message": "not found"})
            return self._ok(self._out(user))
        return httpx.Response(404, json={"message": "Not Found"})


class FakeRebeccaPanel(FakeTokenPanel):
    """Rebecca: Marzban paths plus /api/v2/users and /api/v2/services; an API key works as the token."""

    def __init__(self) -> None:
        super().__init__("marzban")
        self.v2_bodies: list[dict[str, Any]] = []
        self.sources: dict[str, list[str]] = {}
        self.insight_reads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.headers.get("authorization") == f"Bearer {self.token}":
            if path == "/api/v2/services":
                return httpx.Response(200, json={"services": [{"id": 7, "name": "Main"}]})
            if path == "/api/core/access/insights":
                self.insight_reads += 1
                items = [{"user_key": k, "user_label": k, "sources": v} for k, v in self.sources.items()]
                return httpx.Response(200, json={"items": items})
            if path == "/api/v2/users" and request.method == "POST":
                body = json.loads(request.content)
                self.v2_bodies.append(body)
                forwarded = httpx.Request(
                    "POST", request.url.copy_with(path="/api/user"), headers=request.headers, json=body
                )
                return super().handler(forwarded)
            if path.startswith("/api/v2/users/") and request.method == "PUT":
                name = path.rsplit("/", 1)[1]
                forwarded = httpx.Request(
                    "PUT",
                    request.url.copy_with(path=f"/api/user/{name}"),
                    headers=request.headers,
                    content=request.content,
                )
                return super().handler(forwarded)
        return super().handler(request)


class FakeGuardPanel:
    """Guard: X-API-Key, subscriptions created in a list, bulk actions by username."""

    def __init__(self, key: str = "guard-key", version: str = "v1") -> None:
        self.key = key
        self.version = version
        self.subs: dict[str, dict[str, Any]] = {}
        self.fail_next: int | None = None

    def _out(self, sub: dict[str, Any]) -> dict[str, Any]:
        return {**sub, "tag": "sub", "access_key": sub["access_key"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.fail_next:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"detail": "boom"})
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401, json={"detail": "bad key"})
        body = json.loads(request.content) if request.content else {}
        if path == "/api/admins/current":
            return httpx.Response(200, json={"username": "bot"})
        if path == "/api/subscriptions/stats":
            return httpx.Response(200, json={"total": len(self.subs), "active": len(self.subs)})
        if path == "/api/services":
            return httpx.Response(200, json=[{"id": 1, "remark": "Default"}, {"id": 2, "remark": "Gaming"}])
        if path == "/api/subscriptions" and request.method == "POST":
            made = []
            for entry in body:
                if entry["username"] in self.subs:
                    return httpx.Response(409, json={"detail": "exists"})
                sub = {
                    "id": len(self.subs) + 1,
                    "username": entry["username"],
                    "limit_usage": entry.get("limit_usage", 0),
                    "limit_expire": entry.get("limit_expire", 0),
                    "services": entry.get("services", []),
                    "note": entry.get("note"),
                    "telegram_id": entry.get("telegram_id"),
                    "enabled": True,
                    "total_usage": 0,
                    "access_key": secrets.token_hex(6),
                }
                self.subs[sub["username"]] = sub
                made.append(self._out(sub))
            return httpx.Response(200, json=made)
        if path == "/api/subscriptions" and request.method == "GET":
            names = request.url.params.get("usernames")
            rows = [self._out(s) for s in self.subs.values() if not names or s["username"] == names]
            return httpx.Response(200, json={"items": rows, "total": len(rows)})
        if path == "/api/subscriptions" and request.method == "PUT":
            if self.version != "v2":
                return httpx.Response(405, json={"detail": "Method Not Allowed"})
            for name in body.get("usernames", []):
                if name in self.subs:
                    self.subs[name].update(
                        {k: v for k, v in body.items() if k in {"limit_usage", "limit_expire"}}
                    )
            return httpx.Response(
                200, json=[self._out(self.subs[n]) for n in body.get("usernames", []) if n in self.subs]
            )
        if path == "/api/subscriptions" and request.method == "DELETE":
            for name in body.get("usernames", []):
                self.subs.pop(name, None)
            return httpx.Response(200, json={})
        if path.startswith("/api/subscriptions/") and path.rsplit("/", 1)[1] in {
            "enable",
            "disable",
            "reset",
            "revoke",
        }:
            action = path.rsplit("/", 1)[1]
            for name in body.get("usernames", []):
                sub = self.subs.get(name)
                if sub is None:
                    continue
                if action in {"enable", "disable"}:
                    sub["enabled"] = action == "enable"
                elif action == "reset":
                    sub["total_usage"] = 0
                else:
                    sub["access_key"] = secrets.token_hex(6)
            return httpx.Response(200, json={})
        if path.startswith("/api/subscriptions/") and self.version == "v1":
            name = path.rsplit("/", 1)[1]
            sub = self.subs.get(name)
            if sub is None:
                return httpx.Response(404, json={"detail": "not found"})
            if request.method == "GET":
                return httpx.Response(200, json=self._out(sub))
            if request.method == "PUT":
                sub.update({k: v for k, v in body.items() if k in {"limit_usage", "limit_expire"}})
                return httpx.Response(200, json=self._out(sub))
        return httpx.Response(404, json={"detail": "Not Found"})


class FakeHiddifyPanel:
    """Hiddify Manager v2 admin API: Hiddify-API-Key, users by UUID with trailing slashes, usage in GB."""

    def __init__(self, key: str = "admin-uuid") -> None:
        self.key = key
        self.users: dict[str, dict[str, Any]] = {}
        self.fail_next: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.fail_next:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"msg": "boom"})
        if request.headers.get("hiddify-api-key") != self.key:
            return httpx.Response(401, json={"msg": "unauthorized"})
        body = json.loads(request.content) if request.content else {}
        if path == "/admin-path/api/v2/admin/server_status/":
            return httpx.Response(
                200,
                json={"stats": {"system": {"cpu_percent": 9, "num_cpus": 2, "ram_used": 1, "ram_total": 4}}},
            )
        if path == "/admin-path/api/v2/admin/user/" and request.method == "GET":
            return httpx.Response(200, json=list(self.users.values()))
        if path == "/admin-path/api/v2/admin/user/" and request.method == "POST":
            uid = f"{secrets.token_hex(4)}-0000-4000-8000-{secrets.token_hex(6)}"
            user = {"uuid": uid, "current_usage_GB": 0, "enable": True, "last_online": None, **body}
            self.users[uid] = user
            return httpx.Response(200, json=user)
        if path.startswith("/admin-path/api/v2/admin/user/"):
            uid = path.rstrip("/").rsplit("/", 1)[1]
            user = self.users.get(uid)
            if user is None:
                return httpx.Response(404, json={"msg": "not found"})
            if request.method == "GET":
                return httpx.Response(200, json=user)
            if request.method == "PATCH":
                user.update(body)
                return httpx.Response(200, json=user)
            if request.method == "DELETE":
                del self.users[uid]
                return httpx.Response(200, json={"status": 200})
        return httpx.Response(404, json={"msg": "Not Found"})


def transport(
    fake: FakeTokenPanel
    | FakeXuiPanel
    | FakeMarzneshinPanel
    | FakeRemnawavePanel
    | FakeGuardPanel
    | FakeHiddifyPanel,
) -> httpx.MockTransport:
    return httpx.MockTransport(fake.handler)


def now() -> datetime:
    return datetime.now(UTC)
