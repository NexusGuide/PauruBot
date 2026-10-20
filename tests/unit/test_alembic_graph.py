"""Keep independent feature migrations reachable through one deployable head."""

from pathlib import Path

from alembic.script import ScriptDirectory


def test_migration_graph_has_one_complete_head() -> None:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations"))
    heads = scripts.get_heads()
    assert len(heads) == 1, f"Add an explicit merge revision for migration heads: {heads}"
    revisions = list(scripts.walk_revisions())
    assert len({item.revision for item in revisions}) == len(revisions)
    assert {item.revision for item in revisions} >= {"0026", "0026_card_verification"}
    merge = scripts.get_revision("0027_merge_service_cards")
    assert merge is not None
    assert set(merge.down_revision) == {"0026", "0026_card_verification"}
