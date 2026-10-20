"""Recovery notices are translated, fixed security text, not editable shop messages."""

from paurubot.auth import password_reset
from paurubot.i18n import Texts, fields
from paurubot.ops import docgen


def test_builtin_recovery_notices_have_matching_variables():
    for locale in ("en", "fa"):
        assert fields(Texts(locale).raw("auth_password_reset_notice")) == {"username"}


def test_recovery_notice_is_bilingual_escaped_and_has_one_token():
    token = "synthetic-token-not-a-real-secret"
    message = password_reset._message("<owner>&", token)
    assert "Password reset" in message and "بازنشانی رمز" in message
    assert "Never send it to support" in message and "برای پشتیبانی نفرست" in message
    assert "&lt;owner&gt;&amp;" in message and "<owner>" not in message
    assert message.count(token) == 1 and message.endswith(f"<code>{token}</code>")
    assert "5 minutes" in message and "۵ دقیقه" in message


def test_reference_access_distinguishes_recovery_from_password_change():
    assert docgen._access("/api/auth/password", []) == "staff session"
    assert docgen._access("/api/auth/password-reset/request", []) == "public"
    assert docgen._access("/api/auth/password-reset/confirm", []) == "public"
