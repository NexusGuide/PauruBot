"""initData signed by a reseller bot opens that reseller's shop, never the root shop (TEN-04)."""

from __future__ import annotations

import json
import time

import pytest

from paurubot.api.webapp_auth import match_init_data
from paurubot.security.tokens import InitDataError, sign_init_data

MAIN = "111:main-token"
RESELLER_A = "222:reseller-a"
RESELLER_B = "333:reseller-b"


def _init_data(token: str, user_id: int = 42) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "A"})}
    return sign_init_data(fields, token)


def test_main_bot_maps_to_root_shop() -> None:
    user, shop_id = match_init_data(_init_data(MAIN), MAIN, [(7, RESELLER_A)], max_age=3600)
    assert user.id == 42
    assert shop_id is None


def test_reseller_bot_maps_to_its_own_shop() -> None:
    raw = _init_data(RESELLER_B)
    _, shop_id = match_init_data(raw, MAIN, [(7, RESELLER_A), (9, RESELLER_B)], max_age=3600)
    assert shop_id == 9


def test_unknown_bot_is_rejected() -> None:
    with pytest.raises(InitDataError):
        match_init_data(_init_data("444:stranger"), MAIN, [(7, RESELLER_A)], max_age=3600)


def test_expired_reseller_init_data_is_rejected() -> None:
    fields = {"auth_date": str(int(time.time()) - 10_000), "user": json.dumps({"id": 1})}
    with pytest.raises(InitDataError):
        match_init_data(sign_init_data(fields, RESELLER_A), MAIN, [(7, RESELLER_A)], max_age=3600)
