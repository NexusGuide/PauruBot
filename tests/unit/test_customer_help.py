"""F-09: bounded public projection and URL policy shared with frontend fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paurubot.core import customer_help

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "help_urls.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_customer_help_url_policy(case: dict[str, object]) -> None:
    result = customer_help.safe_url(case["url"])
    assert (result is not None) == case["allowed"]
    if result is not None:
        assert result == case["url"]


def test_projection_never_returns_settings_secrets_file_ids_or_unusable_links() -> None:
    result = customer_help.view(
        {
            "help_items": [
                None,
                {"title": "Read", "text": "<script>not HTML</script>", "url": "javascript:bad"},
                {"title": "Bad link only", "url": "http://example.com"},
            ],
            "download_apps": [
                {
                    "name": "App",
                    "platform": "android",
                    "repo": "Owner/App",
                    "pattern": "*.apk",
                    "store_url": "https://example.com/download",
                    "file_id": "PRIVATE_FILE",
                },
                {"name": "Bad", "platform": "android", "repo": "../outside", "store_url": "data:bad"},
            ],
            "zarinpal_merchant": "PAYMENT_SECRET",
            "app_cache_chat_id": 123456,
        },
        bot_username="Shop_bot",
    )
    assert result == {
        "articles": [{"id": 1, "title": "Read", "text": "<script>not HTML</script>", "url": None}],
        "apps": [
            {
                "id": 0,
                "name": "App",
                "platform": "android",
                "store_url": "https://example.com/download",
                "release_url": "https://github.com/Owner/App/releases",
            }
        ],
        "bot_url": "https://t.me/Shop_bot",
    }
    assert "PRIVATE_FILE" not in str(result) and "PAYMENT_SECRET" not in str(result)


def test_projection_is_bounded_and_ignores_malformed_settings() -> None:
    assert customer_help.view({"help_items": "bad", "download_apps": {}}, bot_username=None) == {
        "articles": [],
        "apps": [],
        "bot_url": None,
    }
    result = customer_help.view(
        {
            "help_items": [{"title": "x" * 200, "text": "x" * 9000}] * 40,
            "download_apps": [{"name": "x" * 100, "platform": "ios", "store_url": "https://example.com"}]
            * 30,
        },
        bot_username="bad/name",
    )
    assert len(result["articles"]) == 30 and len(result["apps"]) == 20
    assert len(result["articles"][0]["text"]) == 8000 and len(result["articles"][0]["title"]) == 128
    assert len(result["apps"][0]["name"]) == 32 and result["bot_url"] is None
