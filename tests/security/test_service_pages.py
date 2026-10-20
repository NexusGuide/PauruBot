"""Owned service search is applied before bounded keyset pagination (audit F-01)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from paurubot.db.models import Service, ServiceStatus
from tests import factories
from tests.unit.test_webapp_api import app_env as app_env
from tests.unit.test_webapp_api import h

pytestmark = pytest.mark.security
UID = 990101


@pytest.fixture
async def page_env(session, app_env):  # type: ignore[no-untyped-def]
    response = await app_env["client"].get("/api/webapp/me", headers=h(UID))
    assert response.status_code == 200
    return {**app_env, "owner_id": response.json()["id"]}


async def add(session, env, count: int, **values):  # type: ignore[no-untyped-def]
    rows = [
        Service(
            shop_id=env["shop"].id,
            customer_id=env["owner_id"],
            panel_id=env["panel"].id,
            panel_username=f"page-user-{i}-{values.get('status', 'active')}",
            label=f"Service {i}",
            status=ServiceStatus.ACTIVE,
        )
        for i in range(count)
    ]
    for row in rows:
        for key, value in values.items():
            setattr(row, key, value)
    session.add_all(rows)
    await session.commit()
    return rows


async def get(env, **params):  # type: ignore[no-untyped-def]
    response = await env["client"].get("/api/webapp/services", headers=h(UID), params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_forward_keyset_has_stable_order_and_exact_end(session, page_env) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 23)
    first = await get(page_env, limit=20)
    assert [s["id"] for s in first["items"]] == [r.id for r in rows[::-1][:20]]
    assert first["next_before"] == rows[-20].id
    second = await get(page_env, limit=20, before=first["next_before"])
    assert [s["id"] for s in second["items"]] == [r.id for r in rows[:3][::-1]]
    assert second["next_before"] is None


async def test_legacy_unparameterized_contract_does_not_truncate_home_alerts(session, page_env) -> None:  # type: ignore[no-untyped-def]
    await add(session, page_env, 23)
    result = await get(page_env)
    assert set(result) == {"items"} and len(result["items"]) == 23
    assert len((await get(page_env, q=""))["items"]) == 20


async def test_exact_limit_is_final_page_and_empty_page_has_no_cursor(session, page_env) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 20)
    result = await get(page_env, limit=20)
    assert len(result["items"]) == 20 and result["next_before"] is None
    assert await get(page_env, limit=20, before=rows[0].id) == {"items": [], "next_before": None}


@pytest.mark.parametrize("limit", [1, 50])
async def test_supported_page_size_boundaries(session, page_env, limit) -> None:  # type: ignore[no-untyped-def]
    await add(session, page_env, 51)
    result = await get(page_env, limit=limit)
    assert len(result["items"]) == limit and result["next_before"] == result["items"][-1]["id"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 51},
        {"before": 0},
        {"before": -1},
        {"before": 2**63},
        {"before": "bad"},
        {"q": "x" * 121},
        {"status": "deleted"},
        {"status": "unknown"},
    ],
)
async def test_invalid_filters_are_rejected(page_env, params) -> None:  # type: ignore[no-untyped-def]
    response = await page_env["client"].get("/api/webapp/services", headers=h(UID), params=params)
    assert response.status_code == 422


@pytest.mark.parametrize("q", ["TEHRAN", " تهران ", "legacy-special"])
async def test_search_matches_label_or_username_beyond_the_first_page(session, page_env, q) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 25)
    rows[0].label = "Tehran تهران"
    rows[0].panel_username = "legacy-special"
    await session.commit()
    result = await get(page_env, q=q)
    assert [s["id"] for s in result["items"]] == [rows[0].id]
    assert result["next_before"] is None


@pytest.mark.parametrize("needle", ["%", "_", "\\", "x' OR 1=1 --"])
async def test_search_treats_wildcards_and_sql_metacharacters_literally(session, page_env, needle) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 5)
    rows[0].label = f"literal {needle} only"
    # Prevent the username's intentional underscores from affecting the underscore check.
    for i, row in enumerate(rows):
        row.panel_username = f"safeuser{i}"
    await session.commit()
    result = await get(page_env, q=needle, limit=1)
    assert [s["id"] for s in result["items"]] == [rows[0].id]
    assert result["next_before"] is None


@pytest.mark.parametrize("status", [s for s in ServiceStatus if s != ServiceStatus.DELETED])
async def test_status_filter_applies_before_paging(session, page_env, status) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 25)
    rows[0].status = status
    rows[0].label = "target"
    for row in rows[1:]:
        row.status = ServiceStatus.DISABLED if status != ServiceStatus.DISABLED else ServiceStatus.ACTIVE
    await session.commit()
    result = await get(page_env, status=status.value, limit=1)
    assert [s["id"] for s in result["items"]] == [rows[0].id]
    assert result["next_before"] is None
    assert (await get(page_env, q="absent", status=status.value, limit=1))["items"] == []


async def test_other_customers_other_shops_and_deleted_services_are_never_visible(session, page_env) -> None:  # type: ignore[no-untyped-def]
    owned = (await add(session, page_env, 1))[0]
    owned.label = "shared-keyword"
    other = await factories.customer(session, page_env["shop"])
    shop = await factories.shop(session, "Other tenant")
    panel = await factories.panel(session, shop)
    hidden = [
        Service(
            shop_id=page_env["shop"].id,
            customer_id=other.id,
            panel_id=page_env["panel"].id,
            panel_username="hidden-customer",
            label="shared-keyword",
            status=ServiceStatus.ACTIVE,
        ),
        # Even inconsistent legacy linkage is excluded by the explicit shop predicate.
        Service(
            shop_id=shop.id,
            customer_id=page_env["owner_id"],
            panel_id=panel.id,
            panel_username="hidden-shop",
            label="shared-keyword",
            status=ServiceStatus.ACTIVE,
        ),
        Service(
            shop_id=page_env["shop"].id,
            customer_id=page_env["owner_id"],
            panel_id=page_env["panel"].id,
            panel_username="hidden-deleted",
            label="shared-keyword",
            status=ServiceStatus.DELETED,
        ),
    ]
    session.add_all(hidden)
    await session.commit()
    for params in (
        {},
        {"limit": 1},
        {"q": "shared-keyword", "limit": 1},
        {"status": "active", "limit": 1},
        {"before": hidden[-1].id, "limit": 1},
    ):
        result = await get(page_env, **params)
        assert [s["id"] for s in result["items"]] == [owned.id]
        assert "hidden-" not in str(result)
        assert result.get("next_before") is None


async def test_insert_and_deleted_cursor_anchor_do_not_duplicate_or_skip_remaining_ids(
    session, page_env
) -> None:  # type: ignore[no-untyped-def]
    rows = await add(session, page_env, 12)
    original = [r.id for r in rows[::-1]]
    first = await get(page_env, limit=5)
    seen = [s["id"] for s in first["items"]]
    cursor = first["next_before"]
    anchor = next(r for r in rows if r.id == cursor)
    await session.delete(anchor)
    session.add(
        Service(
            shop_id=page_env["shop"].id,
            customer_id=page_env["owner_id"],
            panel_id=page_env["panel"].id,
            panel_username="new-after-page",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.commit()
    while cursor:
        page = await get(page_env, limit=5, before=cursor)
        seen.extend(s["id"] for s in page["items"])
        cursor = page["next_before"]
    assert seen == original and len(set(seen)) == len(seen)


async def test_missing_auth_cannot_search_or_page(app_env) -> None:  # type: ignore[no-untyped-def]
    result = await app_env["client"].get("/api/webapp/services", params={"q": "target", "limit": 1})
    assert result.status_code == 401


async def test_migration_installs_owner_cursor_index(session) -> None:  # type: ignore[no-untyped-def]
    definition = await session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND indexname='ix_services_owner_page'"
        )
    )
    assert definition and "(shop_id, customer_id, id)" in definition


async def test_index_migration_can_downgrade_and_upgrade_inside_disposable_transaction(session) -> None:  # type: ignore[no-untyped-def]
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def cycle(connection):  # type: ignore[no-untyped-def]
        spec = importlib.util.spec_from_file_location(
            "owner_page_migration",
            Path(__file__).resolve().parents[2] / "migrations/versions/0026_service_owner_page.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname='ix_services_owner_page'"
                    )
                )
                == 0
            )
            module.upgrade()
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname='ix_services_owner_page'"
                    )
                )
                == 1
            )

    connection = await session.connection()
    await connection.run_sync(cycle)
