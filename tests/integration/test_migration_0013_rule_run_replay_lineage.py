"""Upgrade-path contract for persisted replay lineage (T081)."""

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
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, text

from dq.config.settings import Role
from dq.db.schemas import ALL_SCHEMAS, Schema, physical
from dq.db.test_isolation import acquire_session_lock, release_session_lock, sweep

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class UpgradeCase:
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


def _lineage_column(conn: Connection, dq_schema: str) -> object | None:
    return conn.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = 'rule_run' "
            "AND column_name = 'replay_of_rule_run_id'"
        ),
        {"schema": dq_schema},
    ).scalar_one_or_none()


def _restore_historical_0012_shape(conn: Connection, dq_schema: str) -> None:
    """Keep the pre-upgrade schema historical when 0003 imports future metadata."""
    conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
    try:
        conn.execute(
            text(f'ALTER TABLE "{dq_schema}".rule_run DROP COLUMN IF EXISTS replay_of_rule_run_id')
        )
    finally:
        conn.execute(text("RESET ROLE"))


@pytest.fixture
def upgrade_case(test_database_url: str) -> Iterator[UpgradeCase]:
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
        command.upgrade(config, "0012")
        with engine.begin() as conn:
            _restore_historical_0012_shape(conn, physical(Schema.DQ, prefix))
        yield UpgradeCase(engine=engine, config=config, prefix=prefix)
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


def test_upgrade_0012_to_0013_adds_nullable_restrictive_replay_lineage(
    upgrade_case: UpgradeCase,
) -> None:
    with upgrade_case.engine.connect() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0012"
        assert _lineage_column(conn, upgrade_case.dq_schema) is None

    revision = ScriptDirectory.from_config(upgrade_case.config).get_revision("0013")
    assert revision is not None
    assert revision.down_revision == "0012"
    command.upgrade(upgrade_case.config, "0013")

    with upgrade_case.engine.connect() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0013"
        assert _lineage_column(conn, upgrade_case.dq_schema) == "YES"
        foreign_key = conn.execute(
            text("""
                SELECT target.relname AS target_table, constraint_row.confdeltype
                FROM pg_constraint constraint_row
                JOIN pg_class source ON source.oid = constraint_row.conrelid
                JOIN pg_namespace namespace_row ON namespace_row.oid = source.relnamespace
                JOIN pg_class target ON target.oid = constraint_row.confrelid
                WHERE namespace_row.nspname = :schema
                  AND source.relname = 'rule_run'
                  AND constraint_row.contype = 'f'
                  AND pg_get_constraintdef(constraint_row.oid)
                      LIKE 'FOREIGN KEY (replay_of_rule_run_id)%'
            """),
            {"schema": upgrade_case.dq_schema},
        ).one()

    assert foreign_key.target_table == "rule_run"
    assert foreign_key.confdeltype == "r", "replay source deletion must be RESTRICT"

    command.downgrade(upgrade_case.config, "0012")
    with upgrade_case.engine.connect() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0012"
        assert _lineage_column(conn, upgrade_case.dq_schema) is None
