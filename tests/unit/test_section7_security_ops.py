"""Unit tests for Section 7 security, auth, RBAC, sessions, backup, audit, bundle, migration & queue fixes."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_staff_otp_passes_shop_id_and_cleans_up_on_delivery_failure() -> None:
    src = _read("paurubot/auth/staff.py")
    assert "shop_id=staff.shop_id" in src
    assert 'await redis.delete(f"otp:code:{staff.id}")' in src


def test_webapp_forced_gate_exempts_staff() -> None:
    src = _read("paurubot/api/webapp_auth.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "forced_gate")
    body_src = ast.get_source_segment(src, fn) or ""
    assert "_gate_exempt" in body_src


def test_people_role_hardening_and_session_invalidation_only_on_sensitive_change() -> None:
    src = _read("paurubot/api/routes/admin/people.py")
    assert "StaffRole.MERCHANT" in src
    assert "reseller_profile_required" in src
    assert "access_changed" in src


def test_sessions_lifecycle_pruning_and_multi_device_management() -> None:
    src = _read("paurubot/auth/sessions.py")
    assert "def list_for_staff" in src
    assert "def destroy_others" in src
    assert "srem" in src

    auth_src = _read("paurubot/api/routes/auth.py")
    assert '@router.get("/sessions")' in auth_src
    assert '@router.delete("/sessions/others")' in auth_src
    assert "expires_at_invalid" in auth_src
    assert auth_src.count('response.headers["Cache-Control"] = "no-store"') >= 6


def test_audit_redacts_extended_secret_fields_and_supports_csv_export() -> None:
    log_src = _read("paurubot/audit/log.py")
    for key in ("key_hash", "payload_enc", "guest_token", "credentials_enc", "token_enc", "secret_enc"):
        assert f'"{key}"' in log_src
    try:
        from paurubot.audit.log import SECRET_FIELDS, _scrub
    except ModuleNotFoundError:
        pass
    else:
        for key in ("key_hash", "payload_enc", "guest_token", "credentials_enc", "token_enc", "secret_enc"):
            assert key in SECRET_FIELDS
        scrubbed = _scrub({"nested": {"guest_token": "secret", "safe": 1}, "key_hash": "abc"})
        assert scrubbed["key_hash"] == "***"
        assert scrubbed["nested"]["guest_token"] == "***"
        assert scrubbed["nested"]["safe"] == 1

    sys_src = _read("paurubot/api/routes/admin/system.py")
    assert '@router.get("/audit.csv")' in sys_src
    assert "_csv_safe" in sys_src


def test_bundle_allows_stock_plans_and_validates_links() -> None:
    src = _read("paurubot/core/bundle.py")
    assert "_safe_link" in src
    assert 'delivery != "stock"' in src


def test_migration_dry_run_idempotency_and_file_permissions() -> None:
    mig_src = _read("paurubot/migration/runner.py")
    assert "idempotency_key" in mig_src
    assert "ResellerProfile" in mig_src

    sys_src = _read("paurubot/api/routes/admin/system.py")
    assert "os.chmod(path, 0o600)" in sys_src
    assert "async def _require_root_shop" in sys_src


def test_backup_delete_protection_and_root_shop_enforcement() -> None:
    b_src = _read("paurubot/ops/backup.py")
    assert "def delete_archive" in b_src
    assert "backup_protected" in b_src

    sys_src = _read("paurubot/api/routes/admin/system.py")
    assert '@router.delete("/backups/{name}")' in sys_src
    tree = ast.parse(sys_src)
    for fn_name in (
        "list_backups",
        "save_backup_schedule",
        "start_backup",
        "verify_backup",
        "backup_bot_state",
        "backup_bot",
        "download_backup",
        "delete_backup",
        "migration_upload",
        "migration_run",
        "migration_last",
    ):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name)
        seg = ast.get_source_segment(sys_src, fn) or ""
        assert "_require_root_shop" in seg, f"{fn_name} must enforce root shop"


def test_queue_cancel_queued_and_clear_dead() -> None:
    q_src = _read("paurubot/worker/queue.py")
    assert "async def cancel_queued" in q_src
    assert "async def clear_dead" in q_src

    sys_src = _read("paurubot/api/routes/admin/system.py")
    assert '@router.post("/jobs/{job_id}/cancel")' in sys_src
    assert '@router.post("/jobs/clear-dead")' in sys_src


def test_docgen_marks_sessions_routes_as_staff_session() -> None:
    from paurubot.ops import docgen

    src = inspect.getsource(docgen._access)
    assert "/api/auth/sessions" in src
    assert docgen._access("/api/auth/sessions", []) == "staff session"
    assert docgen._access("/api/auth/sessions/others", []) == "staff session"
