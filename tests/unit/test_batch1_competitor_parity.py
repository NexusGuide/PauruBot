"""Unit tests for Batch 1 competitive parity features on 0.1.1-dev."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_alert_policy_supports_warn_remaining_gb():
    from paurubot.core import alert_policy

    gb = 1024**3
    # Percentage off, fixed GB triggers at <= 2 GB
    assert alert_policy.is_low_volume(50 * gb, 48 * gb, 0, warn_gb=2) is True
    assert alert_policy.is_low_volume(50 * gb, 47 * gb, 0, warn_gb=2) is False
    # Both off -> never triggers
    assert alert_policy.is_low_volume(50 * gb, 49 * gb, 0, warn_gb=0) is False
    # Unlimited plan -> never triggers
    assert alert_policy.is_low_volume(0, 5 * gb, 10, warn_gb=2) is False
    # Projection includes warn_remaining_gb
    projected = alert_policy.service_alert_policy(
        {"warn_remaining_percent": 15, "warn_remaining_gb": 3, "warn_days": 2, "warn_time_percent": 20}
    )
    assert projected == {
        "warn_remaining_percent": 15,
        "warn_remaining_gb": 3,
        "warn_days": 2,
        "warn_time_percent": 20,
    }


def test_new_settings_specs_and_source_wiring():
    settings_src = (ROOT / "paurubot/core/settings.py").read_text(encoding="utf-8")
    for key in (
        "discounts_enabled",
        "audit_retention_days",
        "light_mode",
        "trial_hide_after_use",
        "gateway_vpn_off_reminder",
        "reject_reasons",
        "ticket_reminder_minutes",
        "ticket_single_alert",
        "support_faq",
        "warn_remaining_gb",
    ):
        assert f'"{key}": Spec(' in settings_src, f"missing Spec for {key}"


def test_webapp_maintenance_discounts_and_recheck_present():
    webapp_src = (ROOT / "paurubot/api/routes/webapp.py").read_text(encoding="utf-8")
    assert "async def _require_shop_open" in webapp_src
    assert '"shop_maintenance"' in webapp_src
    assert '"discounts_disabled"' in webapp_src
    assert '@router.post("/payments/{payment_id}/recheck")' in webapp_src
    assert '"gateway_vpn_off_reminder"' in webapp_src

    disc_src = (ROOT / "paurubot/api/routes/webapp_discounts.py").read_text(encoding="utf-8")
    assert 'ctx.cfg.get("discounts_enabled", True)' in disc_src


def test_lifecycle_note_includes_username_and_context_injects_balance_ref_and_slow_log():
    lifecycle_src = (ROOT / "paurubot/provisioning/lifecycle.py").read_text(encoding="utf-8")
    assert "tg:{customer.telegram_id} (@{customer.username})" in lifecycle_src

    ctx_src = (ROOT / "paurubot/bot/context.py").read_text(encoding="utf-8")
    assert '"balance": money(bal)' in ctx_src
    assert '"ref_code": f"ref_{customer.telegram_id}"' in ctx_src
    assert "slow telegram update %.2fs" in ctx_src


def test_trial_hide_after_use_and_targeted_reset():
    menu_src = (ROOT / "paurubot/bot/menu.py").read_text(encoding="utf-8")
    assert "trial_used and cfg.get(\"trial_hide_after_use\")" in menu_src

    trials_src = (ROOT / "paurubot/core/trials.py").read_text(encoding="utf-8")
    assert "async def has_used_current(" in trials_src
    assert "async def reset_preview(" in trials_src
    assert "async def reset_non_buyers(" in trials_src

    tools_src = (ROOT / "paurubot/api/routes/admin/tools.py").read_text(encoding="utf-8")
    assert '@router.get("/trial-reset/preview")' in tools_src
    assert 'scope: Literal["all", "non_buyers"] = "all"' in tools_src


def test_ticket_reminders_single_alert_faq_and_preset_reject_reasons():
    jobs_src = (ROOT / "paurubot/worker/jobs.py").read_text(encoding="utf-8")
    assert '@every(86400, "audit.cleanup")' in jobs_src
    assert '@every(300, "tickets.remind")' in jobs_src
    assert "async def _light_mode_skip(" in jobs_src

    handlers_src = (ROOT / "paurubot/notifications/handlers.py").read_text(encoding="utf-8")
    assert "ticket_single_alert" in handlers_src
    assert "async def ticket_reminder(" in handlers_src
    assert '"ticket_reminder": ticket_reminder' in handlers_src

    account_src = (ROOT / "paurubot/bot/routers/account.py").read_text(encoding="utf-8")
    assert "async def ticket_faq(" in account_src
    assert "async def ticket_faq_item(" in account_src

    admin_src = (ROOT / "paurubot/bot/routers/admin.py").read_text(encoding="utf-8")
    assert "def _reject_reason_kb(" in admin_src


def test_dashboard_host_metrics_maintenance_routes_and_repair_cli():
    dash_src = (ROOT / "paurubot/api/routes/admin/dashboard.py").read_text(encoding="utf-8")
    assert "def _host_metrics(" in dash_src
    assert '"host": _host_metrics()' in dash_src

    sys_src = (ROOT / "paurubot/api/routes/admin/system.py").read_text(encoding="utf-8")
    assert '@router.post("/system/maintenance/clear-cache")' in sys_src
    assert '@router.post("/system/maintenance/resync")' in sys_src

    sh_src = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "cmd_repair()" in sh_src
    assert "repair) cmd_repair ;;" in sh_src

    cli_doc = (ROOT / "docs/reference/cli.md").read_text(encoding="utf-8")
    assert "`repair`" in cli_doc


def test_all_locale_pairs_have_exact_key_parity():
    for rel in ("paurubot/i18n", "frontend/src/admin/locales", "frontend/src/app/locales"):
        en = json.loads((ROOT / rel / "en.json").read_text(encoding="utf-8"))
        fa = json.loads((ROOT / rel / "fa.json").read_text(encoding="utf-8"))
        assert set(en) == set(fa), f"Locale mismatch in {rel}: {set(en) ^ set(fa)}"


def test_ticket_miniapp_only_faq_buttons_and_editors():
    settings_src = (ROOT / "paurubot/core/settings.py").read_text(encoding="utf-8")
    assert '"ticket_miniapp_only": Spec(' in settings_src
    assert '"btn_text"' in settings_src and '"btn_url"' in settings_src

    kb_src = (ROOT / "paurubot/bot/keyboards.py").read_text(encoding="utf-8")
    assert "btn_tickets_miniapp" in kb_src
    assert "WebAppInfo(url=miniapp_url)" in kb_src

    account_src = (ROOT / "paurubot/bot/routers/account.py").read_text(encoding="utf-8")
    assert "ticket_miniapp_only" in account_src
    assert "btn_back_faq" in account_src
    assert "/app/tickets" in account_src

    tickets_src = (ROOT / "paurubot/core/tickets.py").read_text(encoding="utf-8")
    assert 'f"ticket_alerted:{ticket.id}"' in tickets_src

    editors_src = (ROOT / "frontend/src/admin/settings-editors.tsx").read_text(encoding="utf-8")
    assert "export function SupportFaqEditor" in editors_src
    assert "export function RejectReasonsEditor" in editors_src

    sys_ui_src = (ROOT / "frontend/src/admin/screens/System.tsx").read_text(encoding="utf-8")
    assert '"support_faq"' in sys_ui_src and '"reject_reasons"' in sys_ui_src
    assert "<SupportFaqEditor" in sys_ui_src and "<RejectReasonsEditor" in sys_ui_src


def test_per_panel_trial_groups_and_builds_status_widget():
    panels_api = (ROOT / "paurubot/api/routes/admin/panels.py").read_text(encoding="utf-8")
    for key in ("trial_group_ids", "trial_service_ids", "trial_squad_ids", "trial_inbounds", "trial_inbound_id"):
        assert f'"{key}"' in panels_api, f"missing {key} in panels.py"

    lifecycle_src = (ROOT / "paurubot/provisioning/lifecycle.py").read_text(encoding="utf-8")
    assert "OrderKind.TRIAL" in lifecycle_src
    assert '"trial_group_ids"' in lifecycle_src
    assert '"trial_squad_ids"' in lifecycle_src

    panels_ui = (ROOT / "frontend/src/admin/screens/Panels.tsx").read_text(encoding="utf-8")
    assert 'mode="trial"' in panels_ui
    assert 'fieldKey="trial_group_ids"' in panels_ui

    sys_api = (ROOT / "paurubot/api/routes/admin/system.py").read_text(encoding="utf-8")
    assert '@router.get("/system/builds")' in sys_api

    build_ui = (ROOT / "frontend/src/admin/screens/BuildStatus.tsx").read_text(encoding="utf-8")
    assert "/api/admin/system/builds" in build_ui
    home_ui = (ROOT / "frontend/src/admin/screens/Home.tsx").read_text(encoding="utf-8")
    assert "<BuildStatus />" in home_ui

    webapp_src = (ROOT / "paurubot/api/routes/webapp.py").read_text(encoding="utf-8")
    assert "LATE_CREDIT" in webapp_src


def test_crons_manager_and_complete_report_topics():
    settings_src = (ROOT / "paurubot/core/settings.py").read_text(encoding="utf-8")
    for topic in ("receipts", "crons", "other"):
        assert f'"{topic}"' in settings_src, f"missing REPORT_TOPICS entry {topic}"
    for key in ("on_hold_remind_hours", "miniapp_notice_expires_at", "cron_overrides"):
        assert f'"{key}": Spec(' in settings_src, f"missing Spec for {key}"

    runner_src = (ROOT / "paurubot/worker/runner.py").read_text(encoding="utf-8")
    assert "CRON_STATE_PREFIX" in runner_src
    assert "CRON_RUN_NOW_PREFIX" in runner_src
    assert "async def get_cron_states(" in runner_src
    assert "async def request_cron_run(" in runner_src

    jobs_src = (ROOT / "paurubot/worker/jobs.py").read_text(encoding="utf-8")
    for name in (
        "services.on_hold_remind",
        "discounts.expire",
        "resellers.expire",
        "miniapp.notice_expire",
    ):
        assert f'"{name}"' in jobs_src, f"missing new cron {name}"

    handlers_src = (ROOT / "paurubot/notifications/handlers.py").read_text(encoding="utf-8")
    assert '"group:receipts"' in handlers_src
    assert '"report_cron_service_alert"' in handlers_src
    assert '"report_other_tier_up"' in handlers_src
    assert '"report_other_ip_replaced"' in handlers_src

    lt_src = (ROOT / "paurubot/core/log_topics.py").read_text(encoding="utf-8")
    assert "async def auto_setup_forum_topics(" in lt_src
    assert "async def send_topic_test(" in lt_src

    ops_src = (ROOT / "paurubot/bot/routers/admin_ops.py").read_text(encoding="utf-8")
    assert 'Command("setuplogs")' in ops_src

    sys_api = (ROOT / "paurubot/api/routes/admin/system.py").read_text(encoding="utf-8")
    assert '@router.post("/channels/logs/auto-setup")' in sys_api
    assert '@router.post("/channels/logs/{row_id}/test")' in sys_api
    assert '@router.get("/system/crons")' in sys_api
    assert '@router.put("/system/crons/{name}")' in sys_api
    assert '@router.post("/system/crons/{name}/run")' in sys_api

    crons_ui = (ROOT / "frontend/src/admin/screens/Crons.tsx").read_text(encoding="utf-8")
    assert "export function CronsSection()" in crons_ui
    assert "/api/admin/system/crons" in crons_ui
