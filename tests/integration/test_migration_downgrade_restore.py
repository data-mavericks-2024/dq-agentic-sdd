"""Verified rollback: downgrade one step from head and restore, in an isolated schema (T089).

**Only one step is meaningful to test.** Several migrations declare an explicit no-op `downgrade()`
— 0006, 0007, 0008, 0010, 0012 — because what they installed is a safety tightening (a predicate
validator, a removed mutable registry) that must never be silently weakened by rolling back past
it. `alembic downgrade base` would not undo those five migrations; it would skip them. The
rollback boundary this feature actually offers a steward is "the most recent migration with a real
downgrade" — 0013 here — not an arbitrary earlier revision, and README.md documents that boundary
rather than promising a full teardown to base.

Starts from a fresh install through every migration in sequence (0001 → head), exactly as the main
suite's own `schema_prefix` fixture does — unlike `test_migration_0013_rule_run_replay_lineage.py`,
which starts *at* 0012 and needs a manual patch to strip the future column Python metadata would
otherwise bring with it. A full sequential run to head, then one step down and back up, needs no
such patch: 0013's `downgrade()` inspects the live schema rather than reflecting Python metadata.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

from dq.config.settings import Role
from dq.db.schemas import ALL_SCHEMAS, Schema, physical
from dq.db.test_isolation import acquire_session_lock, release_session_lock, sweep

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RollbackCase:
    engine: Engine
    config: Config
    prefix: str

    @property
    def dq_schema(self) -> str:
        return physical(Schema.DQ, self.prefix)


def _new_prefix() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"test_{stamp}_{uuid.uuid4().hex[:6]}_"


def _drop_prefixed_schemas(conn: Connection, prefix: str) -> None:
    conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
    try:
        for schema in reversed(ALL_SCHEMAS):
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{physical(schema, prefix)}" CASCADE'))
    finally:
        conn.execute(text("RESET ROLE"))


def _database_revision(conn: Connection, dq_schema: str) -> str:
    return str(
        conn.execute(text(f'SELECT version_num FROM "{dq_schema}".alembic_version')).scalar_one()
    )


def _replay_lineage_shape(conn: Connection, dq_schema: str) -> tuple[object, int]:
    """`(is_nullable, foreign_key_count)` for `rule_run.replay_of_rule_run_id` — 0013's payload."""
    is_nullable = conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = 'rule_run' "
            "AND column_name = 'replay_of_rule_run_id'"
        ),
        {"schema": dq_schema},
    ).scalar_one_or_none()
    fk_count = conn.execute(
        text(
            "SELECT count(*) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = :schema AND t.relname = 'rule_run' AND c.contype = 'f' "
            "  AND pg_get_constraintdef(c.oid) LIKE 'FOREIGN KEY (replay_of_rule_run_id)%'"
        ),
        {"schema": dq_schema},
    ).scalar_one()
    return is_nullable, fk_count


@pytest.fixture
def rollback_case(test_database_url: str) -> Iterator[RollbackCase]:
    engine = create_engine(test_database_url, future=True, pool_pre_ping=True)
    prefix = _new_prefix()
    liveness = engine.connect()
    acquire_session_lock(liveness, prefix)
    sweep(engine)

    previous = os.environ.get("DQ_SCHEMA_PREFIX")
    os.environ["DQ_SCHEMA_PREFIX"] = prefix
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    try:
        command.upgrade(config, "head")
        yield RollbackCase(engine=engine, config=config, prefix=prefix)
    finally:
        if previous is None:
            os.environ.pop("DQ_SCHEMA_PREFIX", None)
        else:
            os.environ["DQ_SCHEMA_PREFIX"] = previous

        try:
            with engine.begin() as conn:
                _drop_prefixed_schemas(conn, prefix)
        finally:
            release_session_lock(liveness, prefix)
            liveness.close()
            engine.dispose()


def test_downgrade_one_step_then_upgrade_restores_head_exactly(
    rollback_case: RollbackCase,
) -> None:
    with rollback_case.engine.connect() as conn:
        assert _database_revision(conn, rollback_case.dq_schema) == "0013"
        before = _replay_lineage_shape(conn, rollback_case.dq_schema)
        assert before == ("YES", 1), "a fresh install to head must already carry 0013's payload"

    command.downgrade(rollback_case.config, "0012")
    with rollback_case.engine.connect() as conn:
        assert _database_revision(conn, rollback_case.dq_schema) == "0012"
        assert _replay_lineage_shape(conn, rollback_case.dq_schema) == (None, 0), (
            "0013's column and foreign key must be fully removed at 0012"
        )

    command.upgrade(rollback_case.config, "0013")
    with rollback_case.engine.connect() as conn:
        assert _database_revision(conn, rollback_case.dq_schema) == "0013"
        after = _replay_lineage_shape(conn, rollback_case.dq_schema)

    assert after == before == ("YES", 1), (
        "restoration to head must reproduce the pre-downgrade shape"
    )
