"""Subscription-link domains (ClockBot parity B4): the owner adds a domain, Caddy may get a
certificate only for a registered domain (on-demand TLS asks first), a domain is checked for DNS,
a valid certificate and that it really reaches this server, and once healthy it can carry the
shop's subscription links. A domain that is not healthy, or removed, never carries links: they
fall back to the main address."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from paurubot.config import get_settings
from paurubot.core import domains
from paurubot.core import settings as shop_settings
from paurubot.db.models import Service, ShopDomain
from paurubot.provisioning import subscription
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _main_address(monkeypatch):  # type: ignore[no-untyped-def]
    # Only where links and domains read it: an https public_url would make the test login cookie Secure.
    main = SimpleNamespace(public_url="https://bot.example.com", master_key=get_settings().master_key)
    monkeypatch.setattr(subscription, "get_settings", lambda: main)
    monkeypatch.setattr(domains, "get_settings", lambda: main)
    shop_settings.invalidate()
    yield
    shop_settings.invalidate()


# ---------------------------------------------------------------- the address itself


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("sub.example.com", "sub.example.com"),
        ("  HTTPS://Sub.Example.COM/some/path?x=1 ", "sub.example.com"),
        ("sub.example.com.", "sub.example.com"),
        ("sub.example.com:443", "sub.example.com"),
        ("a-b.c-d.example.ir", "a-b.c-d.example.ir"),
    ],
)
def test_addresses_are_normalised(raw: str, host: str) -> None:
    assert domains.normalize_host(raw) == host


def test_international_names_become_punycode() -> None:
    assert domains.normalize_host("مثال.ir").startswith("xn--")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "example",
        "1.2.3.4",
        "[::1]",
        "localhost",
        "my.localhost",
        "*.example.com",
        "a..example.com",
        "exa mple.com",
        "ex_ample.com",
        "-bad.example.com",
        "x" * 64 + ".example.com",
        ("a" * 60 + ".") * 5 + "com",
        "sub.example.com:8443",
        "ftp://sub.example.com",
    ],
)
def test_bad_addresses_are_refused(raw: str) -> None:
    with pytest.raises(domains.DomainError):
        domains.normalize_host(raw)


# ---------------------------------------------------------------- who may manage domains


async def test_only_the_owner_adds_and_removes_and_shops_stay_apart(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    admin = await login(http, "u_admin")
    assert (await admin.call("POST", "/api/admin/domains", {"host": "sub.example.com"})).status_code == 403
    listed = await admin.call("GET", "/api/admin/domains")
    assert listed.status_code == 200, "admins may look"

    owner = await login(http, "u_owner")
    made = await owner.call("POST", "/api/admin/domains", {"host": "HTTPS://Sub.Example.com/"})
    assert made.status_code == 200, made.text
    row = made.json()
    assert row["host"] == "sub.example.com" and row["status"] == "pending" and row["for_links"] is False

    again = await owner.call("POST", "/api/admin/domains", {"host": "sub.example.com"})
    assert again.status_code == 409 and again.json()["error"] == "domain_taken"
    main = await owner.call("POST", "/api/admin/domains", {"host": "bot.example.com"})
    assert main.status_code == 422 and main.json()["error"] == "domain_is_main"
    bad = await owner.call("POST", "/api/admin/domains", {"host": "1.2.3.4"})
    assert bad.status_code == 422 and bad.json()["error"] == "domain_invalid"

    other = await login(http, "other_owner")
    assert (await other.call("POST", "/api/admin/domains", {"host": "sub.example.com"})).json()[
        "error"
    ] == "domain_taken"
    theirs = (await other.call("GET", "/api/admin/domains")).json()
    assert theirs["domains"] == [], "another shop never sees this shop's domains"
    assert (await other.call("DELETE", f"/api/admin/domains/{row['id']}")).status_code == 404

    owner = await login(http, "u_owner")
    assert (await owner.call("DELETE", f"/api/admin/domains/{row['id']}")).status_code == 200
    assert (await session.scalars(select(ShopDomain))).all() == []


async def test_a_shop_has_a_limited_number_of_domains(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    for n in range(domains.MAX_PER_SHOP):
        session.add(ShopDomain(shop_id=env.shop.id, host=f"d{n}.example.com"))
    await session.commit()
    owner = await login(http, "u_owner")
    resp = await owner.call("POST", "/api/admin/domains", {"host": "one-more.example.com"})
    assert resp.status_code == 422 and resp.json()["error"] == "domain_limit"


# ---------------------------------------------------------------- certificates only for known names


async def test_caddy_may_get_a_certificate_only_for_a_registered_domain(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    session.add(ShopDomain(shop_id=env.shop.id, host="sub.example.com"))
    await session.commit()
    assert (await http.get("/internal/tls-ask", params={"domain": "sub.example.com"})).status_code == 200
    assert (await http.get("/internal/tls-ask", params={"domain": "SUB.example.com."})).status_code == 200
    assert (await http.get("/internal/tls-ask", params={"domain": "evil.example.com"})).status_code == 404
    assert (await http.get("/internal/tls-ask", params={"domain": "1.2.3.4"})).status_code == 404
    assert (await http.get("/internal/tls-ask")).status_code == 404


async def test_the_server_answers_its_own_mark_on_any_domain(http) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    resp = await http.get("/s/_/domain", headers={"host": "sub.example.com"})
    assert resp.status_code == 200
    assert resp.text == domains.marker("sub.example.com")
    assert domains.marker("sub.example.com") != domains.marker("other.example.com")


# ---------------------------------------------------------------- links


async def test_a_healthy_domain_carries_the_links_and_falls_back_when_it_is_not(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    service = await session.get(Service, env.other_service_id)
    service.sub_token = "t" * 40
    domain = ShopDomain(shop_id=env.shop.id, host="sub.example.com")
    session.add(domain)
    await session.commit()

    owner = await login(http, "u_owner")
    early = await owner.call("PUT", "/api/admin/domains/links", {"domain_id": domain.id})
    assert early.status_code == 409 and early.json()["error"] == "domain_not_ready"

    domain.status = domains.OK
    await session.commit()
    assert (await owner.call("PUT", "/api/admin/domains/links", {"domain_id": domain.id})).status_code == 200

    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert cfg["link_domain"] == "sub.example.com"
    assert subscription.branded_url(service, cfg) == f"https://sub.example.com/s/{'t' * 40}"
    assert subscription.customer_link(
        service, {**cfg, "branded_sub_links": True}
    ) == subscription.branded_url(service, cfg)
    other_cfg = await shop_settings.load(session, env.other_shop.id, fresh=True)
    assert not other_cfg.get("link_domain"), "another shop keeps its own address"

    domain.status = domains.TLS
    await session.commit()
    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert not cfg.get("link_domain"), "a failing domain never carries links"
    assert subscription.branded_url(service, cfg) == f"https://bot.example.com/s/{'t' * 40}"

    domain.status = domains.OK
    await session.commit()
    assert (await owner.call("PUT", "/api/admin/domains/links", {"domain_id": None})).status_code == 200
    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert not cfg.get("link_domain"), "back to the main address"

    assert (await owner.call("PUT", "/api/admin/domains/links", {"domain_id": domain.id})).status_code == 200
    assert (await owner.call("DELETE", f"/api/admin/domains/{domain.id}")).status_code == 200
    cfg = await shop_settings.load(session, env.shop.id, fresh=True)
    assert not cfg.get("link_domain"), "a removed domain stops carrying links"


# ---------------------------------------------------------------- the health check


class FakeProbe:
    def __init__(self, *, addresses=("203.0.113.7",), tls=None, mark=None, error_at=None):  # type: ignore[no-untyped-def]
        self.addresses, self.tls, self.mark, self.error_at = addresses, tls, mark, error_at

    async def resolve(self, host: str) -> list[str]:
        if self.error_at == "dns":
            raise OSError("no such host")
        return list(self.addresses)

    async def certificate(self, host: str) -> domains.CertInfo:
        if self.error_at == "tls":
            raise domains.ProbeError("certificate verify failed")
        if self.error_at == "unreachable":
            raise domains.ProbeError("connection refused", unreachable=True)
        return self.tls or domains.CertInfo(
            expires_at=datetime(2027, 2, 1, tzinfo=UTC), issuer="Let's Encrypt"
        )

    async def fetch_mark(self, host: str) -> str:
        return self.mark if self.mark is not None else domains.marker(host)


@pytest.mark.parametrize(
    ("probe", "status"),
    [
        (FakeProbe(), domains.OK),
        (FakeProbe(error_at="dns"), domains.DNS),
        (FakeProbe(addresses=("10.0.0.5",)), domains.DNS),
        (FakeProbe(addresses=("127.0.0.1",)), domains.DNS),
        (FakeProbe(error_at="tls"), domains.TLS),
        (FakeProbe(error_at="unreachable"), domains.UNREACHABLE),
        (FakeProbe(mark="someone-else"), domains.NOT_OURS),
    ],
)
async def test_each_failure_has_its_own_status(probe: FakeProbe, status: str) -> None:
    result = await domains.check("sub.example.com", probe=probe)
    assert result.status == status
    if status == domains.OK:
        assert result.issuer == "Let's Encrypt" and result.expires_at is not None and result.error is None
    else:
        assert result.error


async def test_the_scheduled_check_updates_every_domain(session, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    good = ShopDomain(shop_id=env.shop.id, host="good.example.com")
    bad = ShopDomain(shop_id=env.other_shop.id, host="bad.example.com", status=domains.OK)
    session.add_all([good, bad])
    await session.commit()

    class Mixed(FakeProbe):
        async def resolve(self, host: str) -> list[str]:
            if host.startswith("bad"):
                raise OSError("gone")
            return ["203.0.113.7"]

    assert await domains.run_checks(session, probe=Mixed()) == 2
    await session.refresh(good)
    await session.refresh(bad)
    assert good.status == domains.OK and good.cert_issuer == "Let's Encrypt" and good.checked_at is not None
    assert good.cert_expires_at is not None and good.cert_expires_at > datetime.now(UTC) + timedelta(days=1)
    assert bad.status == domains.DNS and bad.error


async def test_check_now_is_owner_only_and_paced(session, http, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    domain = ShopDomain(shop_id=env.shop.id, host="sub.example.com")
    session.add(domain)
    await session.commit()
    monkeypatch.setattr(domains, "default_probe", lambda: FakeProbe())
    admin = await login(http, "u_admin")
    assert (await admin.call("POST", f"/api/admin/domains/{domain.id}/check")).status_code == 403
    owner = await login(http, "u_owner")
    first = await owner.call("POST", f"/api/admin/domains/{domain.id}/check")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == domains.OK
    second = await owner.call("POST", f"/api/admin/domains/{domain.id}/check")
    assert second.status_code == 429 and second.json()["error"] == "domain_check_too_soon"
