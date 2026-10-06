from __future__ import annotations

import re
from pathlib import Path

import pytest

from paurubot.db.models import JobStatus, OrderKind, ServiceStatus, TicketStatus
from paurubot.i18n import LOCALES, Texts, catalog, fields, render, validate_override

ROOT = Path(__file__).resolve().parents[2] / "paurubot"
LITERAL = re.compile(
    r"""\b(?:t|new_t|texts)\(\s*"([a-z0-9_]+)"|Btn\(([^)]*)\)|matches\([^,]+,\s*"([a-z0-9_]+)"\)"""
)
PERSIAN = re.compile("[\u0600-\u06ff]")


def used_keys() -> set[str]:
    keys: set[str] = set()
    for path in ROOT.rglob("*.py"):
        for match in LITERAL.finditer(path.read_text(encoding="utf-8")):
            if match.group(1):
                keys.add(match.group(1))
            elif match.group(2):
                keys.update(re.findall(r'"([a-z0-9_]+)"', match.group(2)))
            elif match.group(3):
                keys.add(match.group(3))
    keys |= {f"status_{s.value}" for s in ServiceStatus}
    keys |= {f"order_kind_{k.value}" for k in OrderKind}
    keys |= {f"checkout_header_{k.value}" for k in OrderKind}
    keys |= {f"ticket_status_{s.value}" for s in TicketStatus}
    keys |= {f"btn_pay_{m}" for m in ("card", "zarinpal", "stars")}
    keys |= {f"lang_{code}" for code in LOCALES}
    keys |= {f"tx_{k}" for k in ("topup", "charge", "refund", "reward", "adjustment")}
    keys |= {f"alert_{a}" for a in ("low_volume", "expiring", "expired", "limited", "deleted")}
    keys |= {
        "choose_renew_plan",
        "choose_upgrade_plan",
        "choose_extra_gb",
        "choose_extra_days",
        "extra_gb_option",
        "extra_days_option",
    }
    assert JobStatus.DRAFT  # keep the import meaningful
    return keys


def test_every_used_key_exists_in_every_locale() -> None:
    keys = used_keys()
    assert len(keys) > 200
    for locale in LOCALES:
        missing = sorted(keys - set(catalog(locale)))
        assert not missing, f"{locale} is missing {missing}"


def test_locales_have_same_keys_and_placeholders() -> None:
    fa, en = catalog("fa"), catalog("en")
    assert set(fa) == set(en)
    for key in fa:
        assert fields(fa[key]) == fields(en[key]), key


def test_no_persian_in_python_code() -> None:
    offenders = [str(p) for p in ROOT.rglob("*.py") if PERSIAN.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_render_is_safe_against_format_injection() -> None:
    assert render("{name.__class__}", {"name": "x"}) == "{name.__class__}"
    assert render("{0}", {"name": "x"}) == "{0}"
    assert render("hi {name}", {"name": "Ali"}) == "hi Ali"


def test_overrides_and_validation() -> None:
    t = Texts("en", {"en": {"welcome": "Yo {name}!"}}, {"name": "Sam", "shop": "S"})
    assert t("welcome") == "Yo Sam!"
    assert Texts("fa", {}, {"name": "x", "shop": "S"})("welcome").startswith("سلام")
    assert validate_override("plan_button", "{title}: {price}") == []
    assert validate_override("plan_button", "{title} {secret}") == ["unknown variable {secret}"]
    assert validate_override("plan_button", "{title") == ["unbalanced braces"]


@pytest.mark.parametrize("locale", LOCALES)
def test_button_matching_works_across_languages(locale: str) -> None:
    t = Texts(locale)
    for other in LOCALES:
        assert t.matches(Texts(other)("btn_buy"), "btn_buy")
    assert not t.matches("random text", "btn_buy")
