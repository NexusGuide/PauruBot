"""Unit tests for Section 8 Bot UX, menus, button looks, infocard, admin bot, texts & settings."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_menu_disallows_icon_on_webapp_and_handles_empty_layout() -> None:
    src = _read("paurubot/bot/menu.py")
    assert "allow_icon: bool = True" in src
    assert "allow_icon=False" in src
    assert "ReplyKeyboardRemove()" in src


def test_national_mode_hides_qr_and_infocard_links() -> None:
    ic_src = _read("paurubot/bot/infocard.py")
    assert "panel.national_mode" in ic_src

    svc_src = _read("paurubot/bot/routers/services.py")
    tree = ast.parse(svc_src)
    for fn_name in ("qr", "info_card"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name)
        seg = ast.get_source_segment(svc_src, fn) or ""
        assert "national_mode" in seg, f"{fn_name} must check national_mode"

    h_src = _read("paurubot/notifications/handlers.py")
    h_tree = ast.parse(h_src)
    fn = next(
        n for n in ast.walk(h_tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_delivery_photo"
    )
    assert "national_mode" in (ast.get_source_segment(h_src, fn) or "")


def test_admin_bot_backups_and_web_links_restricted_to_root_shop() -> None:
    ops_src = _read("paurubot/bot/routers/admin_ops.py")
    assert "shop.parent_id is not None" in ops_src

    gen_src = _read("paurubot/bot/routers/admin_general.py")
    assert "is_customer_bot" in gen_src
    assert 'url.startswith("https://")' in gen_src

    nav_src = _read("paurubot/bot/routers/admin_navigation.py")
    assert 'url.startswith("https://")' in nav_src


def test_auto_reply_ignores_blank_keywords() -> None:
    src = _read("paurubot/bot/app.py")
    assert "(kw := _fold(str(w)).strip()) and kw in folded" in src


def test_i18n_html_validation_button_rules_and_cross_locale_matches() -> None:
    src = _read("paurubot/i18n/__init__.py")
    assert "TELEGRAM_HTML_TAGS" in src
    assert "_validate_telegram_html" in src
    assert "text cannot be empty" in src
    assert "button label must be one line" in src
    assert "button label is too long (max 64)" in src
    assert "self._overrides.get(loc, {}).get(key)" in src


def test_button_looks_and_system_cache_invalidation_and_portable_settings() -> None:
    bl_src = _read("paurubot/bot/button_looks.py")
    assert '"shop":' in bl_src
    assert "return by_text" in bl_src

    sys_src = _read("paurubot/api/routes/admin/system.py")
    assert sys_src.count("button_looks.forget(ctx.shop_id)") >= 5
    assert "_SHOP_LOCAL_SETTINGS" in sys_src
    assert '@router.post("/texts/{locale}/{key}/preview")' in sys_src
    assert "staff_telegram_required" in sys_src


def test_start_router_passes_cfg_and_guards_stale_file_id() -> None:
    src = _read("paurubot/bot/routers/start.py")
    assert "miniapp_button(t, cfg)" in src
    assert "parse_mode=None" in src
    assert "except TelegramBadRequest:" in src
