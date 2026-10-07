"""The admin panel's texts and in-panel help stay complete and consistent (ADM-05, QA-03)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from paurubot.api.routes.admin.system import LOG_TOPICS
from paurubot.auth.rbac import PERMISSIONS
from paurubot.core.settings import SPECS

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "frontend" / "src" / "admin"
HELP = ADMIN / "help"
LOCALES = ("fa", "en")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
SCREENS = re.compile(r"^<!--\s*screens:\s*(.*?)\s*-->$", re.M)
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _locale(name: str) -> dict[str, str]:
    data: dict[str, str] = json.loads((ADMIN / "locales" / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _routes() -> set[str]:
    source = (ADMIN / "App.tsx").read_text(encoding="utf-8")
    return {p for p in re.findall(r'<Route path="([^"]+)"', source) if p != "*" and ":" not in p}


def _articles(locale: str) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted((HELP / locale).glob("*.md"))}


def test_admin_locales_match() -> None:
    fa, en = _locale("fa"), _locale("en")
    assert set(fa) == set(en)
    for key in fa:
        assert set(PLACEHOLDER.findall(fa[key])) == set(PLACEHOLDER.findall(en[key])), key


def test_texts_built_from_server_values_exist() -> None:
    en = _locale("en")
    missing = [f"perm_{p.replace('.', '_')}" for p in PERMISSIONS if f"perm_{p.replace('.', '_')}" not in en]
    missing += [f"log_topic_{t}" for t in LOG_TOPICS if f"log_topic_{t}" not in en]
    shown = {
        k: s for k, s in SPECS.items() if s.group != "appearance"
    }  # appearance lives on the bot menu screen
    missing += [f"setting_{k}" for k in shown if f"setting_{k}" not in en]
    missing += sorted({f"settings_group_{s.group}" for s in shown.values()} - set(en))
    doctor = (ROOT / "paurubot" / "ops" / "doctor.py").read_text(encoding="utf-8")
    missing += sorted({f"doctor_{k}" for k in re.findall(r'add\("([a-z_]+)"', doctor)} - set(en))
    assert missing == []


def test_every_article_exists_in_both_languages() -> None:
    fa, en = _articles("fa"), _articles("en")
    assert len(fa) >= 10
    assert set(fa) == set(en)
    for slug in fa:
        assert SCREENS.findall(fa[slug]) == SCREENS.findall(en[slug]), slug
        heads = [len(re.findall(r"^## ", text, re.M)) for text in (fa[slug], en[slug])]
        assert heads[0] == heads[1], f"{slug}: sections differ {heads}"
        assert fa[slug].startswith("# ") and en[slug].startswith("# "), slug


@pytest.mark.parametrize("locale", LOCALES)
def test_article_links_and_screens_point_somewhere_real(locale: str) -> None:
    routes = _routes()
    articles = _articles(locale)
    owner: dict[str, str] = {}
    for slug, text in articles.items():
        for screen in " ".join(SCREENS.findall(text)).split():
            assert screen in routes, f"{slug}: unknown screen {screen}"
            assert screen not in owner, f"{screen} is claimed by {owner.get(screen)} and {slug}"
            owner[screen] = slug
        for _label, href in LINK.findall(text):
            if href.startswith("help:"):
                assert href[5:].split("#")[0] in articles, f"{slug}: no article {href}"
            else:
                assert href in routes, f"{slug}: no screen {href}"


@pytest.mark.parametrize("locale", LOCALES)
def test_articles_use_only_the_supported_markup(locale: str) -> None:
    for slug, text in _articles(locale).items():
        body = SCREENS.sub("", text)
        assert "<" not in re.sub(r"`[^`]*`", "", body).replace("<!--", ""), f"{slug}: raw HTML"
        assert "‎" not in text and "‏" not in text, f"{slug}: direction marks"
        assert not re.search(r"^[ \t]+[-*]\s", body, re.M), f"{slug}: nested list"


def test_quoted_labels_match_the_panel() -> None:
    """Bold English labels in help must be real button or field names (or plain emphasis we allow)."""
    labels = set(_locale("en").values())
    words = {re.sub(r"\s+", " ", re.sub(r"\(?\{\w+\}\)?", "", v)).strip() for v in labels}
    allowed = {
        "Location",
        "Category",
        "Owner",
        "Admin",
        "Finance",
        "Support",
        "Reseller",
        "Type",
        "Limits",
        "Sticky",
        "Customers",
        "Wallet balances",
        "Resellers",
        "Services",
        "Card transfer",
        "Crypto",
        "Zarinpal",
        "Telegram Stars",
        "NowPayments and Plisio",
        "Prepaid",
        "By usage",
        "Hourly",
        "Required channels",
        "Report channels",
        "Support contacts",
        "Look and naming",
        "Extra data and time",
        "Start on first connection",
        "Panel settings for this plan",
        "Errors",
        "Change location",
        "Buy several",
        "Panels",
        "Backups",
        "Daily report",
        "Everything",
        "Last action",
        "Healthy",
        "OK",
        "Warning",
        "Problem",
    }
    unknown: list[str] = []
    for slug, text in _articles("en").items():
        for label in re.findall(r"\*\*(.+?)\*\*", text):
            plain = label.rstrip(":")
            if plain not in words and plain not in allowed:
                unknown.append(f"{slug}: {plain}")
    assert unknown == []
