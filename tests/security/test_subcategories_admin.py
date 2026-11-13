"""Staff put a category under another one (one level deep), from the admin panel."""

from __future__ import annotations

import pytest

from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_a_category_goes_under_another_one_level_deep(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    volume = (await owner.call("POST", "/api/admin/categories", {"title": "VOLUME", "sort": 0})).json()
    per_user = await owner.call(
        "POST", "/api/admin/categories", {"title": "PER-USER", "sort": 0, "parent_id": volume["id"]}
    )
    assert per_user.status_code == 200, per_user.text
    assert per_user.json()["parent_id"] == volume["id"]

    deeper = await owner.call(
        "POST", "/api/admin/categories", {"title": "X", "sort": 0, "parent_id": per_user.json()["id"]}
    )
    assert deeper.status_code == 422 and deeper.json()["error"] == "category_too_deep"

    under_itself = await owner.call(
        "PUT",
        f"/api/admin/categories/{volume['id']}",
        {"title": "VOLUME", "sort": 0, "parent_id": volume["id"]},
    )
    assert under_itself.status_code == 422

    with_children = await owner.call(
        "PUT",
        f"/api/admin/categories/{volume['id']}",
        {"title": "VOLUME", "sort": 0, "parent_id": per_user.json()["id"]},
    )
    assert with_children.status_code == 422 and with_children.json()["error"] == "category_too_deep"

    listed = (await owner.call("GET", "/api/admin/catalog")).json()["categories"]
    assert {c["title"]: c["parent_id"] for c in listed} == {"VOLUME": None, "PER-USER": volume["id"]}
    accounts = (await owner.call("GET", "/api/admin/account-plans")).json()["categories"]
    assert {c["title"]: c["parent_id"] for c in accounts} == {"VOLUME": None, "PER-USER": volume["id"]}

    # deleting the category keeps its subcategories, now standing on their own
    assert (await owner.call("DELETE", f"/api/admin/categories/{volume['id']}")).status_code == 200
    listed = (await owner.call("GET", "/api/admin/catalog")).json()["categories"]
    assert [(c["title"], c["parent_id"]) for c in listed] == [("PER-USER", None)]


async def test_another_shops_category_is_not_a_parent(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    other = await login(http, "other_owner")
    theirs = (await other.call("POST", "/api/admin/categories", {"title": "THEIRS", "sort": 0})).json()
    owner = await login(http, "u_owner")
    resp = await owner.call(
        "POST", "/api/admin/categories", {"title": "MINE", "sort": 0, "parent_id": theirs["id"]}
    )
    assert resp.status_code == 404
