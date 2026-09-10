"""The convergence migrations extend, rather than rewrite, the applied chain."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _literal_assignment(path: Path, name: str) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            assert node.value is not None
            value = ast.literal_eval(node.value)
            assert isinstance(value, str | type(None))
            return value
    raise AssertionError(f"{name} not found in {path}")


def test_registry_cleanup_extends_revision_0009() -> None:
    migration = REPO_ROOT / "migrations" / "versions" / "0010_remove_test_session_registry.py"

    assert migration.is_file()
    assert _literal_assignment(migration, "revision") == "0010"
    assert _literal_assignment(migration, "down_revision") == "0009"


def test_composite_normalization_extends_revision_0010() -> None:
    migration = REPO_ROOT / "migrations" / "versions" / "0011_composite_normalization_index.py"

    assert migration.is_file()
    assert _literal_assignment(migration, "revision") == "0011"
    assert _literal_assignment(migration, "down_revision") == "0010"


def test_historical_watermark_validation_extends_revision_0011() -> None:
    migration = REPO_ROOT / "migrations" / "versions" / "0012_historical_watermark_validation.py"

    assert migration.is_file()
    assert _literal_assignment(migration, "revision") == "0012"
    assert _literal_assignment(migration, "down_revision") == "0011"


def test_rule_run_replay_lineage_extends_revision_0012() -> None:
    migration = REPO_ROOT / "migrations" / "versions" / "0013_rule_run_replay_lineage.py"

    assert migration.is_file()
    assert _literal_assignment(migration, "revision") == "0013"
    assert _literal_assignment(migration, "down_revision") == "0012"
