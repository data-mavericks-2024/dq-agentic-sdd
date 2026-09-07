"""Upgrade-path contract for historical delivery watermark validation (T080)."""

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
from sqlalchemy.exc import DBAPIError

from dq.config.settings import Role
from dq.db.schemas import ALL_SCHEMAS, Schema, physical
from dq.db.test_isolation import acquire_session_lock, release_session_lock, sweep

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

UNBOUNDED_BATCH_PREDICATE = """
SELECT b.batch_id::text AS subject_key,
       b.arrival_ts::text AS offending_value,
       NULL::text AS observed_value,
       NULL::text AS expected_value
FROM   data_batch b
WHERE  b.source_system_id = :scope_source_system_id
"""


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


def _restore_historical_0011_validator(conn: Connection, dq_schema: str) -> None:
    """Represent a database that applied 0011 before T080 expanded Rule 7.

    Earlier migrations import the live replaceable-object module, so rebuilding 0011 after the
    production fix would otherwise install the future validator and make the upgrade test vacuous.
    """
    conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
    try:
        conn.execute(
            text(
                f'''CREATE OR REPLACE FUNCTION "{dq_schema}".assert_predicate_valid(
                        predicate_sql text, parameters jsonb, subject_type text
                    ) RETURNS void
                    LANGUAGE plpgsql
                    AS $fn$
                    BEGIN
                        RETURN;
                    END;
                    $fn$'''
            )
        )
    finally:
        conn.execute(text("RESET ROLE"))


def _validate(conn: Connection, dq_schema: str) -> None:
    conn.execute(
        text(
            f'SELECT "{dq_schema}".assert_predicate_valid('
            "CAST(:predicate AS text), CAST('{}' AS jsonb), 'record')"
        ),
        {"predicate": UNBOUNDED_BATCH_PREDICATE},
    )


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
        command.upgrade(config, "0011")
        with engine.begin() as conn:
            _restore_historical_0011_validator(conn, physical(Schema.DQ, prefix))
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


def test_upgrade_0011_to_0012_installs_historical_watermark_validation(
    upgrade_case: UpgradeCase,
) -> None:
    with upgrade_case.engine.begin() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0011"
        _validate(conn, upgrade_case.dq_schema)

    revision = ScriptDirectory.from_config(upgrade_case.config).get_revision("0012")
    assert revision is not None
    assert revision.down_revision == "0011"
    command.upgrade(upgrade_case.config, "0012")

    with upgrade_case.engine.begin() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0012"
        with pytest.raises(DBAPIError, match="Rule 7"):
            _validate(conn, upgrade_case.dq_schema)
