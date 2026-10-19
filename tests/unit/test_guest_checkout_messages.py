"""Guest replay errors use the regular bilingual catalogues and retain fallback behavior."""

from paurubot.api.routes.guest import _intent_notice
from paurubot.i18n import Texts, fields

ERRORS = {
    "payment_intent_invalid": "guest_checkout_form_invalid",
    "payment_intent_conflict": "guest_checkout_form_conflict",
    "payment_intent_closed": "guest_checkout_form_closed",
}


def test_guest_intent_errors_are_translated_in_each_locale():
    for error, key in ERRORS.items():
        en, fa = Texts("en"), Texts("fa")
        assert en.raw(key) != key and fa.raw(key) != key
        assert en.raw(key) != fa.raw(key)
        assert fields(en.raw(key)) == fields(fa.raw(key)) == set()
        assert _intent_notice(en, error) == en(key)
        assert _intent_notice(fa, error) == fa(key)


def test_other_guest_errors_keep_translation_and_gateway_fallback():
    t = Texts("en")
    assert _intent_notice(t, "guest_method_unavailable") == t("guest_method_unavailable")
    assert _intent_notice(t, "unknown_guest_error_for_test") == t("gateway_error")
