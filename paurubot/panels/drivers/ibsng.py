"""IBSng: the classic RADIUS accounting panel, driven through its admin web pages (PNL-11).

IBSng has no API, so this signs in as an admin and reads and posts the same forms a person would.
Each plan maps to an IBSng group, which holds the real limits; the bot creates the account with a
generated password, reads it back, and deletes it. Renewing, disabling and resetting usage stay
in IBSng itself. Page markup differs between IBSng builds: anything unexpected is reported as an
error rather than guessed at.
"""

from __future__ import annotations

import html
import re
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import quote

from paurubot.panels.base import (
    GB,
    UNSET,
    PanelDriver,
    PanelError,
    PanelHealth,
    PanelNotFound,
    PanelOption,
    PanelRef,
    PanelUser,
    UserSpec,
)

ADMIN = "/IBSng/admin"
MB = 1024 * 1024
_UID_FROM_ADD = re.compile(r"user_id=(\d+)")
_UID_FROM_INFO = re.compile(r"change_credit\.php\?user_id=(\d+)")
_GROUP = re.compile(r'group_info\.php\?group_name=([^"&]+)')
_PASSWORD = re.compile(r'id="password"[^>]*value="([^"]*)"')
_CELL = r"{label}.*?<td class=\"Form_Content_Row_Right_[a-z_]+\">\s*(.*?)\s*</td>"
_AMOUNT = re.compile(r"([\d.,]+)\s*([GMK]?)", re.IGNORECASE)


def _cell(page: str, label: str) -> str:
    match = re.search(_CELL.format(label=re.escape(label)), page, re.DOTALL)
    value = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else ""
    return "" if set(value) <= {"-"} else value


def _bytes(text: str, default_unit: int = GB) -> int:
    match = _AMOUNT.search(text or "")
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = {"G": GB, "M": MB, "K": 1024}.get(match.group(2).upper(), default_unit)
    return int(value * unit)


class IbsngDriver(PanelDriver):
    kind = "ibsng"
    supports_webhooks = False
    delivery = "account"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._signed_in = False

    async def _login(self) -> None:
        resp = await self.http.request(
            "POST", f"{ADMIN}/", data={"username": self.username, "password": self.password}
        )
        if resp.status_code >= 400 or "admin_index" not in resp.text:
            raise PanelError("login failed", resp.status_code)
        self._signed_in = True

    async def page(self, path: str, data: dict[str, Any] | None = None) -> str:
        """POST a form (IBSng posts even to read) and return the page, signing in again once."""
        if not self._signed_in:
            await self._login()
        for attempt in range(2):
            resp = await self.http.request("POST", path, data=data or {})
            login_form = 'name="username"' in resp.text and "admin_index" not in resp.text
            expired = resp.status_code in (301, 302, 401, 403) or login_form
            if expired and attempt == 0:
                await self._login()
                continue
            if resp.status_code >= 400:
                raise PanelError(f"{path} failed", resp.status_code)
            return resp.text
        raise PanelError("session could not be established")

    def _group(self, overrides: dict[str, Any] | None = None) -> str:
        group = str({**self.options, **(overrides or {})}.get("group") or "")
        if not group:
            raise PanelError("no IBSng group is set for this plan or panel")
        return group

    async def _info(self, username: str) -> str:
        page = await self.page(f"{ADMIN}/user/user_info.php?normal_username_multi={quote(username)}")
        if "does not exist" in page:
            raise PanelNotFound(f"user {username} not found", 404)
        return page

    async def _password(self, uid: str) -> str:
        page = await self.page(
            f"{ADMIN}/plugins/edit.php",
            {"user_id": uid, "edit_user": 1, "attr_edit_checkbox_2": "normal_username"},
        )
        match = _PASSWORD.search(page)
        return html.unescape(match.group(1)) if match else ""

    def _parse(self, username: str, page: str, password: str | None = None) -> PanelUser:
        uid_match = _UID_FROM_INFO.search(page)
        group = _GROUP.search(page)
        data: dict[str, Any] = {"id": uid_match.group(1) if uid_match else _cell(page, "User ID")}
        if group:
            data["group"] = html.unescape(group.group(1))
        if password is not None:
            data["password"] = password
        locked = _cell(page, "Lock").lower().startswith("yes")
        return PanelUser(
            username=username,
            status="disabled" if locked else "active",
            used_bytes=_bytes(_cell(page, "Used Traffic") or _cell(page, "Traffic Usage"), MB),
            data=data,
            limits_known=False,
        )

    # --- protocol -----------------------------------------------------------

    async def health_check(self) -> PanelHealth:
        self._signed_in = False
        await self._login()
        return PanelHealth(True, "ok", None, None)

    async def list_options(self) -> list[PanelOption]:
        return []

    async def create_user(self, spec: UserSpec) -> PanelUser:
        group = self._group(spec.options)
        added = await self.page(
            f"{ADMIN}/user/add_new_users.php",
            {
                "submit_form": 1,
                "add": 1,
                "count": 1,
                "credit": "1",
                "owner_name": self.username,
                "group_name": group,
                "edit__normal_username": 1,
            },
        )
        uid = _UID_FROM_ADD.search(added)
        if not uid:
            raise PanelError("IBSng did not return the new user id")
        password = secrets.token_hex(6)
        saved = await self.page(
            f"{ADMIN}/plugins/edit.php?edit_user=1&user_id={uid.group(1)}&submit_form=1&add=1&count=1"
            f"&credit=1&owner_name=system&group_name={quote(group)}&x=35&y=1"
            "&edit__normal_username=normal_username",
            {
                "target": "user",
                "target_id": uid.group(1),
                "update": 1,
                "edit_tpl_cs": "normal_username",
                "attr_update_method_0": "normalAttrs",
                "has_normal_username": "t",
                "current_normal_username": "",
                "normal_username": spec.username,
                "password": password,
                "normal_save_user_add": "t",
                "credit": "1",
            },
        )
        if "exist" in saved and "user_info.php?user_id_multi" not in saved:
            raise PanelError("that username is already taken on IBSng")
        if "user_info.php?user_id_multi" not in saved:
            raise PanelError("IBSng did not confirm the new user")
        return self._parse(spec.username, await self._info(spec.username), password)

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            page = await self._info(ref.username)
        except PanelNotFound:
            return None
        password = ref.data.get("password")
        return self._parse(ref.username, page, str(password) if password else None)

    async def update_user(
        self,
        ref: PanelRef,
        *,
        expire_at: datetime | None = UNSET,
        data_limit: int | None = None,
        enabled: bool | None = None,
    ) -> PanelUser:
        if enabled is False:
            raise PanelError("IBSng accounts are locked from IBSng itself")
        info = await self.get_user(ref)
        if info is None:
            raise PanelNotFound(f"user {ref.username} not found", 404)
        return info  # limits live in the IBSng group; the bot records what was sold

    async def reset_usage(self, ref: PanelRef) -> None:
        return None  # IBSng counts usage against the group's limits by itself

    async def delete_user(self, ref: PanelRef) -> None:
        try:
            page = await self._info(ref.username)
        except PanelNotFound:
            return
        uid = _UID_FROM_INFO.search(page)
        if not uid:
            raise PanelError("could not read the IBSng user id")
        done = await self.page(
            f"{ADMIN}/user/del_user.php",
            {
                "user_id": uid.group(1),
                "delete": 1,
                "delete_comment": "",
                "delete_connection_logs": "on",
                "delete_audit_logs": "on",
            },
        )
        if "Successfully" not in done:
            raise PanelError("IBSng did not confirm the deletion")

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        raise PanelError("change the password in IBSng itself")

    async def fetch_password(self, ref: PanelRef) -> str:
        page = await self._info(ref.username)
        uid = _UID_FROM_INFO.search(page)
        return await self._password(uid.group(1)) if uid else ""
