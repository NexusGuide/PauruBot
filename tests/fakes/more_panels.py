"""In-memory fakes of WGDashboard, WG-Mate, MikroTik User Manager and IBSng (PNL-08, PNL-11)."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx


class FakeWGDashboard:
    def __init__(self, key: str = "wg-key") -> None:
        self.key = key
        self.peers: dict[str, dict[str, Any]] = {}  # public key -> peer
        self.restricted: set[str] = set()
        self.free = ["10.8.0.2/32", "10.8.0.3/32", "10.8.0.4/32"]
        self.fail_next: int | None = None
        self.calls: list[tuple[str, str, Any]] = []

    def _ok(self, data: Any = None) -> httpx.Response:
        return httpx.Response(200, json={"status": True, "message": None, "data": data})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, text="error")
        if request.headers.get("wg-dashboard-apikey") != self.key:
            return httpx.Response(401, json={"status": False, "message": "unauthorized"})
        if path == "/api/getWireguardConfigurationInfo":
            normal = [p for k, p in self.peers.items() if k not in self.restricted]
            limited = [p for k, p in self.peers.items() if k in self.restricted]
            return self._ok({"configurationPeers": normal, "configurationRestrictedPeers": limited})
        if path == "/api/getAvailableIPs/wg0":
            return self._ok({"10.8.0.0/24": list(self.free)})
        if path == "/api/addPeers/wg0":
            ip = body["allowed_ips"][0]
            self.free.remove(ip)
            self.peers[body["public_key"]] = {
                "id": body["public_key"],
                "name": body["name"],
                "allowed_ip": ip,
                "private_key": body["private_key"],
                "preshared_key": body["preshared_key"],
                "total_data": 0.0,
                "cumu_data": 0.0,
                "jobs": [],
            }
            return self._ok()
        if path == "/api/savePeerScheduleJob":
            job = body["Job"]
            self.peers[job["Peer"]]["jobs"].append(job)
            return self._ok([job])
        if path == "/api/deletePeerScheduleJob":
            job = body["Job"]
            peer = self.peers[job["Peer"]]
            peer["jobs"] = [j for j in peer["jobs"] if j["JobID"] != job["JobID"]]
            return self._ok()
        if path == "/api/restrictPeers/wg0":
            self.restricted.update(body["peers"])
            return self._ok()
        if path == "/api/allowAccessPeers/wg0":
            self.restricted.difference_update(body["peers"])
            return self._ok()
        if path == "/api/resetPeerData/wg0":
            self.peers[body["id"]]["total_data"] = 0.0
            self.peers[body["id"]]["cumu_data"] = 0.0
            return self._ok()
        if path == "/api/deletePeers/wg0":
            for key in body["peers"]:
                peer = self.peers.pop(key, None)
                if peer:
                    self.free.append(peer["allowed_ip"])
            return self._ok()
        if path == "/api/downloadPeer/wg0":
            peer = self.peers.get(request.url.params.get("id", ""))
            if peer is None:
                return httpx.Response(200, json={"status": False, "message": "Peer does not exist"})
            conf = (
                f"[Interface]\nPrivateKey = {peer['private_key']}\nAddress = {peer['allowed_ip']}\n\n"
                f"[Peer]\nPublicKey = SERVER\nPresharedKey = {peer['preshared_key']}\nEndpoint = vpn.example.com:51820\n"
            )
            return self._ok({"fileName": peer["name"], "file": conf})
        return httpx.Response(404, json={"status": False, "message": "not found"})


class FakeWGMate:
    def __init__(self, key: str = "mate-key") -> None:
        self.key = key
        self.users: dict[str, dict[str, Any]] = {}
        self.fail_next: int | None = None
        self.bodies: list[dict[str, Any]] = []
        self._ids = iter(range(1, 10_000))
        self.users["template"] = {
            "id": "t0",
            "username": "template",
            "protocols": ["wireguard", "vless"],
            "serverId": "srv-1",
            "status": "active",
            "subToken": "tpl",
        }

    def _by_id(self, user_id: str) -> dict[str, Any] | None:
        return next((u for u in self.users.values() if str(u["id"]) == user_id), None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, text="error")
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401, json={"error": "invalid token"})
        body = json.loads(request.content) if request.content else {}
        if path == "/api/auth/me":
            return httpx.Response(200, json={"data": {"username": "admin"}})
        if path.startswith("/api/users/by-username/"):
            user = self.users.get(unquote(path.rsplit("/", 1)[1]))
            return (
                httpx.Response(200, json={"data": user})
                if user
                else httpx.Response(404, json={"error": "nf"})
            )
        if path == "/api/users" and request.method == "POST":
            self.bodies.append(body)
            user = {
                **body,
                "id": str(next(self._ids)),
                "status": "active",
                "usedBytes": 0,
                "subToken": secrets.token_hex(6),
                "connection": "offline",
            }
            self.users[body["username"]] = user
            return httpx.Response(201, json={"data": user})
        parts = path.split("/")
        if len(parts) >= 4 and parts[2] == "users":
            user = self._by_id(parts[3])
            if user is None:
                return httpx.Response(404, json={"error": "nf"})
            action = parts[4] if len(parts) > 4 else ""
            if request.method == "PATCH":
                user.update(body)
            elif request.method == "DELETE":
                del self.users[user["username"]]
            elif action == "disable":
                user.update(status="disabled", disableReason="admin")
            elif action == "enable":
                user.update(status="active", disableReason=None)
            elif action == "reset":
                user["usedBytes"] = 0
            elif action == "revoke":
                user["subToken"] = secrets.token_hex(6)
            return httpx.Response(200, json={"data": user})
        return httpx.Response(404, json={"error": "not found"})


class FakeMikrotik:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.assignments: list[tuple[str, str]] = []
        self.usage: dict[str, int] = {}
        self.fail_next: int | None = None
        self._ids = iter(range(1, 10_000))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, json={"error": status, "message": "Internal", "detail": "boom"})
        expected = "Basic " + base64.b64encode(b"admin:panel-password").decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": 401, "message": "Unauthorized"})
        body = json.loads(request.content) if request.content else {}
        um = "/rest/user-manager"
        if path == "/rest/system/resource":
            return httpx.Response(
                200,
                json={
                    "version": "7.15",
                    "cpu-load": "3",
                    "cpu-count": "4",
                    "total-memory": "1073741824",
                    "free-memory": "536870912",
                },
            )
        if path == f"{um}/profile":
            return httpx.Response(
                200, json=[{".id": "*1", "name": "monthly"}, {".id": "*2", "name": "weekly"}]
            )
        if path == f"{um}/user" and request.method == "GET":
            name = request.url.params.get("name")
            rows = [u for u in self.users.values() if name is None or u["name"] == name]
            return httpx.Response(200, json=rows)
        if path == f"{um}/user/add":
            if body["name"] in self.users:
                return httpx.Response(400, json={"error": 400, "message": "Bad Request", "detail": "exists"})
            self.users[body["name"]] = {
                ".id": f"*{next(self._ids):X}",
                "name": body["name"],
                "password": body["password"],
                "disabled": "false",
            }
            return httpx.Response(200, json={"ret": "*1"})
        if path == f"{um}/user-profile/add":
            self.assignments.append((body["user"], body["profile"]))
            return httpx.Response(200, json={"ret": "*9"})
        user = next((u for u in self.users.values() if u[".id"] == body.get(".id")), None)
        if path == f"{um}/user/monitor":
            return httpx.Response(
                200,
                json=[
                    {
                        "total-upload": "100",
                        "total-download": str(self.usage.get(user["name"], 0) if user else 0),
                    }
                ],
            )
        if user is None:
            return httpx.Response(
                400, json={"error": 400, "message": "Bad Request", "detail": "no such item"}
            )
        if path == f"{um}/user/set":
            if "disabled" in body:
                user["disabled"] = "true" if body["disabled"] == "yes" else "false"
            if "password" in body:
                user["password"] = body["password"]
            return httpx.Response(200, json=[])
        if path == f"{um}/user/remove":
            del self.users[user["name"]]
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": 404, "message": "Not Found"})


def ibsng_info(uid: int, username: str, group: str) -> str:
    """The bits of IBSng's user_info page the driver reads."""
    return f"""<html><body><table>
<tr><td class="Form_Content_Row_Left_light">User ID</td><td class="Form_Content_Row_Right_light"> {uid} </td></tr>
<tr><td>Group</td><td><a href="/IBSng/admin/group/group_info.php?group_name={group}">{group}</a></td></tr>
<tr><td class="Form_Content_Row_Left_dark">Lock</td><td class="Form_Content_Row_Right_dark">No</td></tr>
<tr><td class="Form_Content_Row_Left_userinfo_light"><nobr>	    Traffic Limit</nobr></td>
<td class="Form_Content_Row_Right_userinfo_light">20 G</td></tr>
<tr><td>Used Traffic</td><td class="Form_Content_Row_Right_userinfo_dark">512 M</td></tr>
</table><a href="/IBSng/admin/user/change_credit.php?user_id={uid}">credit</a> {username}</body></html>"""


class FakeIbsng:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.pending: dict[int, str] = {}
        self.session = False
        self.fail_next: int | None = None
        self._ids = iter(range(100, 10_000))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        if self.fail_next is not None:
            status, self.fail_next = self.fail_next, None
            return httpx.Response(status, text="error")
        if path == "/IBSng/admin/":
            ok = form.get("username") == "admin" and form.get("password") == "panel-password"
            self.session = ok
            text = "<a href='admin_index.php'>home</a>" if ok else '<form><input name="username"></form>'
            return httpx.Response(
                200, text=text, headers={"set-cookie": "IBS_SESSID=abc; Path=/"} if ok else {}
            )
        if not self.session or "IBS_SESSID=abc" not in request.headers.get("cookie", ""):
            return httpx.Response(200, text='<form><input name="username"><input name="password"></form>')
        if path == "/IBSng/admin/user/add_new_users.php":
            uid = next(self._ids)
            self.pending[uid] = form["group_name"]
            return httpx.Response(
                200, text=f"<a href='/IBSng/admin/user/user_info.php?user_id={uid}&su'>ok</a>"
            )
        if path == "/IBSng/admin/plugins/edit.php" and form.get("normal_username"):
            uid = int(form["target_id"])
            if form["normal_username"] in self.users:
                return httpx.Response(200, text="username already exist")
            self.users[form["normal_username"]] = {
                "uid": uid,
                "password": form["password"],
                "group": self.pending.pop(uid),
            }
            return httpx.Response(
                200, text="<a href='IBSng/admin/user/user_info.php?user_id_multi=1'>done</a>"
            )
        if path == "/IBSng/admin/plugins/edit.php":
            user = next((u for u in self.users.values() if str(u["uid"]) == form.get("user_id")), None)
            value = user["password"] if user else ""
            return httpx.Response(
                200,
                text=f'<td class="Form_Content_Row_Right_light">	<input type=text id="password" name="password" value="{value}">',
            )
        if path == "/IBSng/admin/user/user_info.php":
            name = request.url.params.get("normal_username_multi", "")
            user = self.users.get(name)
            if user is None:
                return httpx.Response(200, text="User does not exists")
            return httpx.Response(200, text=ibsng_info(user["uid"], name, user["group"]))
        if path == "/IBSng/admin/user/del_user.php":
            name = next((n for n, u in self.users.items() if str(u["uid"]) == form.get("user_id")), None)
            if name:
                del self.users[name]
                return httpx.Response(200, text="Successfully deleted")
            return httpx.Response(200, text="error")
        return httpx.Response(404, text="not found")
