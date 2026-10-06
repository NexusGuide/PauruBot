"""Moving over from MirzaBot/Faoxima dumps and WizWiz SQLite files (MIG-01, OPS-08)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from paurubot.db.models import Customer, PanelKind, ResellerProfile, Service, ServiceStatus
from paurubot.ledger.accounts import wallet_balance
from paurubot.migration import readers, runner, sources
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport

MIRZA_DUMP = r"""-- MySQL dump 10.13
DROP TABLE IF EXISTS `user`;
CREATE TABLE `user` (
  `id` varchar(500) NOT NULL,
  `username` varchar(500) NOT NULL DEFAULT '',
  `number` varchar(300) NOT NULL DEFAULT 'none',
  `Balance` int NOT NULL DEFAULT '0',
  `User_Status` varchar(500) NOT NULL DEFAULT 'Active',
  `agent` varchar(100) NOT NULL DEFAULT 'f',
  `namecustom` varchar(300) NOT NULL DEFAULT 'none',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `user` VALUES ('111111111','sara','989121234567',150000,'Active','f','Sara\'s phone'),('222222222','ali_r','none',0,'Active','n','none'),
('333333333','','none',5000,'block','f','none');
CREATE TABLE `invoice` (
  `id_invoice` varchar(200) NOT NULL,
  `id_user` varchar(200) DEFAULT NULL,
  `username` varchar(300) DEFAULT NULL,
  `Service_location` varchar(300) DEFAULT NULL,
  `name_product` varchar(200) DEFAULT NULL,
  `Status` varchar(200) DEFAULT NULL
);
INSERT INTO `invoice` VALUES ('a1','111111111','sara_1','Germany','Monthly 50GB','active'),('a2','111111111','sara_2','Germany','Monthly','end_of_volume'),('a3','222222222','ali_1','Germany',NULL,'Unpaid'),('a4','222222222','ali_2','Netherlands',NULL,'active'),('a5','222222222','gone_1','Germany',NULL,'active');
CREATE TABLE `marzban_panel` (
  `id` int NOT NULL,
  `name_panel` varchar(2000) DEFAULT NULL,
  `url_panel` varchar(2000) DEFAULT NULL
);
INSERT INTO `marzban_panel` (`id`, `name_panel`, `url_panel`) VALUES (1,'Germany','https://de.example.com'),(2,'Netherlands','https://nl.example.com');
"""


@pytest.fixture(autouse=True)
def clean_registry():  # type: ignore[no-untyped-def]
    yield
    registry.test_transports.clear()
    registry._cache.clear()


def test_mysql_dump_reader_handles_escapes_and_column_lists(tmp_path: Path) -> None:
    dump = tmp_path / "old.sql"
    dump.write_text(MIRZA_DUMP)
    rows = list(readers.rows(dump, {"user", "marzban_panel"}))
    users = [r for t, r in rows if t == "user"]
    assert users[0]["namecustom"] == "Sara's phone" and users[0]["Balance"] == 150000
    assert len(users) == 3
    panels = [r for t, r in rows if t == "marzban_panel"]
    assert panels[1] == {"id": 2, "name_panel": "Netherlands", "url_panel": "https://nl.example.com"}
    assert sources.guess(dump) == "mirza"


async def test_mirza_import_dry_run_then_apply(session, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    dump = tmp_path / "old.sql"
    dump.write_text(MIRZA_DUMP)
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    for name, used, limit in (("sara_1", 2 * GB, 50 * GB), ("sara_2", 10 * GB, 10 * GB)):
        fake.users[name] = {
            "id": len(fake.users) + 1,
            "username": name,
            "status": "active",
            "used_traffic": used,
            "data_limit": limit,
            "expire": 0,
            "sub": name,
            "note": "",
            "request": {},
        }
    await session.commit()

    extract = sources.mirza(dump)
    info = runner.summary(extract)
    assert info["customers"] == 3 and info["services"] == 4 and info["balance_total"] == 155000
    assert {p["key"] for p in info["panels"]} == {"Germany", "Netherlands"}

    dry = await runner.apply(session, shop.id, extract, {"Germany": panel.id}, dry_run=True)
    assert (dry.customers_new, dry.services_imported, dry.services_unmapped, dry.services_not_on_panel) == (
        3,
        2,
        1,
        1,
    )
    assert await session.scalar(select(Customer).where(Customer.telegram_id == 111111111)) is None

    report = await runner.apply(session, shop.id, extract, {"Germany": panel.id})
    assert report.services_imported == 2 and report.balance_credited == 155000 and report.resellers == 1
    sara = await session.scalar(select(Customer).where(Customer.telegram_id == 111111111))
    assert sara is not None and sara.phone == "989121234567"
    assert await wallet_balance(session, shop.id, sara.id) == 150000
    services = {
        s.panel_username: s
        for s in await session.scalars(select(Service).where(Service.customer_id == sara.id))
    }
    assert services["sara_2"].status == ServiceStatus.LIMITED and services["sara_1"].label == "Monthly 50GB"
    banned = await session.scalar(select(Customer).where(Customer.telegram_id == 333333333))
    assert banned is not None and banned.is_banned
    ali = await session.scalar(select(Customer).where(Customer.telegram_id == 222222222))
    assert (
        await session.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == ali.id)) is not None
    )

    again = await runner.apply(session, shop.id, extract, {"Germany": panel.id})
    assert again.services_imported == 0 and again.services_existing == 2 and again.balance_credited == 0
    assert await wallet_balance(session, shop.id, sara.id) == 150000


def test_wizwiz_sqlite_extract(tmp_path: Path) -> None:
    db = tmp_path / "wizwiz.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (userid INTEGER, name TEXT, username TEXT, wallet INTEGER, phone TEXT, is_agent INTEGER);
        CREATE TABLE orders_list (id INTEGER, userid INTEGER, server_id INTEGER, remark TEXT, status INTEGER);
        CREATE TABLE server_info (id INTEGER, title TEXT);
        INSERT INTO users VALUES (444444444, 'Mina', 'mina_k', 20000, NULL, 1);
        INSERT INTO orders_list VALUES (1, 444444444, 7, 'mina-1', 1), (2, 444444444, 7, 'mina-old', 0);
        INSERT INTO server_info VALUES (7, 'Turkey');
        """
    )
    conn.commit()
    conn.close()
    assert readers.detect(db) == "sqlite" and sources.guess(db) == "wizwiz"
    extract = sources.wizwiz(db)
    assert extract.customers[444444444].reseller and extract.customers[444444444].balance == 20000
    assert [s.username for s in extract.services] == ["mina-1"] and extract.skipped == 1
    assert extract.panels["7"] == "Turkey (server 7)"


async def test_upload_and_queue_through_the_admin_api(session, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.config import get_settings
    from paurubot.db.models import Job
    from tests.unit.test_bulk import _owner

    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path))
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    await session.commit()
    async with _owner(session, shop) as (client, csrf):
        resp = await client.post(
            "/api/admin/migration/upload",
            files={"file": ("old.sql", MIRZA_DUMP.encode(), "application/sql")},
            headers={"x-csrf-token": csrf},
        )
        assert resp.status_code == 200, resp.text
        info = resp.json()
        assert info["source"] == "mirza" and info["customers"] == 3
        bad = await client.post(
            "/api/admin/migration/upload",
            files={"file": ("x.sql", b"hello world", "application/sql")},
            headers={"x-csrf-token": csrf},
        )
        assert bad.status_code == 422
        run = await client.post(
            "/api/admin/migration/run",
            json={"token": info["token"], "panel_map": {"Germany": panel.id}, "dry_run": True},
            headers={"x-csrf-token": csrf},
        )
        assert run.status_code == 200
    job = await session.get(Job, run.json()["job_id"])
    assert job is not None and job.kind == "migrate" and job.payload["panel_map"] == {"Germany": panel.id}
