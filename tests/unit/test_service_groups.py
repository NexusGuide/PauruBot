"""F-19: the Mini App's services, grouped by location, with the group a real list filter."""

from __future__ import annotations

from paurubot.db.models import Location, PanelKind, Service, ServiceStatus
from paurubot.panels import GB
from tests import factories
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env


def _service(shop, customer_id: int, panel, plan, name: str, status=ServiceStatus.ACTIVE) -> Service:  # type: ignore[no-untyped-def]
    return Service(
        shop_id=shop.id,
        customer_id=customer_id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username=name,
        status=status,
        data_limit=10 * GB,
    )


async def test_groups_count_by_location_and_filter_the_list(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan, home = app_env["client"], app_env["shop"], app_env["plan"], app_env["panel"]
    me = (await client.get("/api/webapp/me", headers=h(93001))).json()
    other = (await client.get("/api/webapp/me", headers=h(93002))).json()
    berlin = Location(shop_id=shop.id, title="Berlin", flag="DE")
    amsterdam = Location(shop_id=shop.id, title="Amsterdam", flag="NL")
    session.add_all([berlin, amsterdam])
    await session.flush()
    de = await factories.panel(session, shop, PanelKind.MARZBAN)
    de.location_id = berlin.id
    nl = await factories.panel(session, shop, PanelKind.MARZBAN)
    nl.location_id = amsterdam.id
    home.location_id = None  # a panel with no location
    mine = [
        _service(shop, me["id"], de, plan, "g-de-1"),
        _service(shop, me["id"], de, plan, "g-de-2", ServiceStatus.EXPIRED),
        _service(shop, me["id"], nl, plan, "g-nl-1"),
        _service(shop, me["id"], home, plan, "g-home-1"),
        _service(shop, me["id"], nl, plan, "g-nl-gone", ServiceStatus.DELETED),
    ]
    session.add_all([*mine, _service(shop, other["id"], de, plan, "g-theirs")])
    await session.commit()

    groups = (await client.get("/api/webapp/services/groups", headers=h(93001))).json()["groups"]
    assert groups == [
        {
            "id": amsterdam.id,
            "title": "Amsterdam",
            "flag": "NL",
            "count": 1,
        },  # the deleted one is not counted
        {"id": berlin.id, "title": "Berlin", "flag": "DE", "count": 2},
        {"id": 0, "title": None, "flag": None, "count": 1},  # no location, last
    ]
    # Nobody else's services are counted, and panel names never leave the server.
    assert (await client.get("/api/webapp/services/groups", headers=h(93002))).json()["groups"] == [
        {"id": berlin.id, "title": "Berlin", "flag": "DE", "count": 1}
    ]

    async def names(**params: object) -> list[str]:
        query = {"limit": 20, **params}
        response = await client.get("/api/webapp/services", params=query, headers=h(93001))
        assert response.status_code == 200, response.text
        return [item["username"] for item in response.json()["items"]]

    assert await names(group=berlin.id) == ["g-de-2", "g-de-1"]
    assert await names(group=0) == ["g-home-1"]
    assert await names(group=berlin.id, status="active") == ["g-de-1"]
    assert await names(group=berlin.id, q="de-2") == ["g-de-2"]
    assert await names(group=amsterdam.id) == ["g-nl-1"]
    assert await names(group=999_999) == []  # an unknown location is just empty
    # Another customer filtering by my location sees only their own.
    response = await client.get("/api/webapp/services", params={"group": berlin.id}, headers=h(93002))
    assert [item["username"] for item in response.json()["items"]] == ["g-theirs"]
    assert "next_before" in response.json()  # a group alone makes the read paged
    for bad in (-1, 2**31, "x"):
        response = await client.get("/api/webapp/services", params={"group": bad}, headers=h(93001))
        assert response.status_code == 422


async def test_a_group_pages_with_its_own_cursor(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan, home = app_env["client"], app_env["shop"], app_env["plan"], app_env["panel"]
    me = (await client.get("/api/webapp/me", headers=h(93011))).json()
    place = Location(shop_id=shop.id, title="Paris")
    session.add(place)
    await session.flush()
    there = await factories.panel(session, shop, PanelKind.MARZBAN)
    there.location_id = place.id
    # Interleave so the other location's services sit between this group's ids.
    for i in range(25):
        session.add(_service(shop, me["id"], there, plan, f"p-{i:02d}"))
        session.add(_service(shop, me["id"], home, plan, f"h-{i:02d}"))
        await session.flush()
    await session.commit()
    seen: list[str] = []
    before = None
    while True:
        params: dict[str, object] = {"limit": 20, "group": place.id}
        if before is not None:
            params["before"] = before
        page = (await client.get("/api/webapp/services", params=params, headers=h(93011))).json()
        seen += [item["username"] for item in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == [f"p-{i:02d}" for i in reversed(range(25))]
