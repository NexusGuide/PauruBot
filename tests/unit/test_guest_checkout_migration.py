"""Exercise the new migration in isolation; not a PostgreSQL/full-chain rehearsal."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_guest_identity_upgrade_tombstones_unique_key_and_downgrade():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0027_guest_checkout_intents.py"
    spec = importlib.util.spec_from_file_location("guest_identity_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            for name in ("shops", "customers", "orders"):
                connection.exec_driver_sql(f"CREATE TABLE {name} (id BIGINT PRIMARY KEY)")
                connection.exec_driver_sql(f"INSERT INTO {name} (id) VALUES (1)")
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()
            table = sa.Table("guest_checkout_intents", sa.MetaData(), autoload_with=connection)
            connection.execute(
                table.insert().values(
                    token_digest="a" * 64,
                    shop_id=1,
                    plan_id=99,
                    method="aqayepardakht",
                    customer_id=1,
                    order_id=1,
                    created_at=datetime.now(UTC),
                )
            )
            with connection.begin_nested():
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        table.insert().values(
                            token_digest="a" * 64,
                            shop_id=1,
                            plan_id=99,
                            method="aqayepardakht",
                            created_at=datetime.now(UTC),
                        )
                    )
            connection.exec_driver_sql("DELETE FROM orders WHERE id=1")
            connection.exec_driver_sql("DELETE FROM customers WHERE id=1")
            row = connection.execute(sa.select(table)).mappings().one()
            assert row["customer_id"] is None and row["order_id"] is None
            assert row["token_digest"] == "a" * 64 and row["plan_id"] == 99
            with Operations.context(context):
                migration.downgrade()
            assert "guest_checkout_intents" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
