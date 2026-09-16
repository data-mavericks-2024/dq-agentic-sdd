"""`dq admin sweep-test-schemas` CLI contract (T071).

`sweep()` itself is exhaustively tested against a real database in
`tests/integration/test_test_schema_liveness.py`. This only covers what the CLI layer adds: option
parsing, the `TEST_DATABASE_URL`-unset error path, and output rendering — none of which need a
database.

Every patched name below is imported *inside* `admin_sweep_test_schemas` at call time, not at
module load — `dq.cli` never binds `load_dotenv`, `create_engine`, or `sweep` as its own
attributes. Patching `cli.load_dotenv` (etc.) would silently patch nothing; the source modules
themselves are what the function's local `from ... import ...` re-reads on every invocation.
"""

from __future__ import annotations

import sqlalchemy
from click.testing import CliRunner
from pytest import MonkeyPatch

from dq import cli
from dq.config import settings as settings_mod
from dq.db import test_isolation as test_isolation_mod


class _FakeEngine:
    def dispose(self) -> None:
        pass


def test_missing_test_database_url_is_reported_without_a_traceback(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda: None)

    result = CliRunner().invoke(cli.main, ["admin", "sweep-test-schemas"])

    assert result.exit_code != 0
    assert "TEST_DATABASE_URL is unset" in result.output


def test_older_than_is_translated_to_a_timedelta(monkeypatch: MonkeyPatch) -> None:
    from datetime import timedelta

    captured: dict[str, object] = {}

    def fake_sweep(engine: object, *, stale_after: timedelta) -> list[str]:
        captured["stale_after"] = stale_after
        return []

    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://example/test")
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda: None)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(test_isolation_mod, "sweep", fake_sweep)

    result = CliRunner().invoke(cli.main, ["admin", "sweep-test-schemas", "--older-than", "0"])

    assert result.exit_code == 0, result.output
    assert captured["stale_after"] == timedelta(hours=0)
    assert "no stale test schemas found" in result.output


def test_dropped_schemas_are_listed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://example/test")
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda: None)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(
        test_isolation_mod, "sweep", lambda engine, **k: ["test_20260101000000_abcdef_commercial"]
    )

    result = CliRunner().invoke(cli.main, ["admin", "sweep-test-schemas"])

    assert result.exit_code == 0, result.output
    assert "dropped test_20260101000000_abcdef_commercial" in result.output
    assert "1 schema(s) dropped" in result.output
