"""docs/reference is generated from the source and must be regenerated with it (QA-03)."""

from __future__ import annotations

from paurubot.ops import docgen


def test_reference_docs_are_current() -> None:
    assert docgen.main(["--check"]) == 0, "run: python -m paurubot.ops.docgen"


def test_reference_covers_every_setting_route_and_command() -> None:
    from paurubot.cli.main import COMMANDS
    from paurubot.core.settings import SPECS

    pages = docgen.render()
    assert all(f"`{key}`" in pages["settings.md"] for key in SPECS)
    assert "`/api/admin/updates`" in pages["api.md"] and "`backups.manage`" in pages["api.md"]
    assert all(f"`{name}`" in pages["cli.md"] for name in COMMANDS)
    assert "secret" in pages["settings.md"] and "zarinpal_merchant" in pages["settings.md"]
