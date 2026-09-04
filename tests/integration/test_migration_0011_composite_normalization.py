"""Upgrade-path contract for the normalized HCP composite index (T079)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, text

from dq.config.settings import Role
from dq.db.schemas import ALL_SCHEMAS, Schema, physical
from dq.db.test_isolation import acquire_session_lock, release_session_lock, sweep
from dq.domain.commercial import COMPOSITE_MATCH_INDEX

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_STRIP_SPACE_AND_PUNCTUATION = "'[[:space:][:punct:]]+', '', 'g'"
_NORMALIZED_LAST_NAME = (
    f'regexp_replace(lower(last_name COLLATE "C"), {_STRIP_SPACE_AND_PUNCTUATION}) COLLATE "C"'
)
_NORMALIZED_FIRST_INITIAL = (
    f'left(regexp_replace(lower(first_name COLLATE "C"), '
    f"{_STRIP_SPACE_AND_PUNCTUATION}), 1) " + 'COLLATE "C"'
)
_NORMALIZED_POSTAL_CODE = (
    f'left(regexp_replace(lower(postal_code COLLATE "C"), '
    f"{_STRIP_SPACE_AND_PUNCTUATION}), 5) " + 'COLLATE "C"'
)
_NORMALIZED_LICENCE_STATE = (
    f'regexp_replace(lower(licence_state COLLATE "C"), {_STRIP_SPACE_AND_PUNCTUATION}) COLLATE "C"'
)


class IndexState(NamedTuple):
    oid: int
    key_attributes: str
    collations: tuple[str, ...]
    is_unique: bool


@dataclass(frozen=True, slots=True)
class UpgradeCase:
    engine: Engine
    config: Config
    prefix: str

    @property
    def commercial_schema(self) -> str:
        return physical(Schema.COMMERCIAL, self.prefix)

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


def _index_state(conn: Connection, schema: str) -> IndexState:
    row = conn.execute(
        text("""
            SELECT i.indexrelid::bigint AS oid,
                   i.indkey::text AS key_attributes,
                   array_agg(coll.collname ORDER BY key.position) AS collations,
                   i.indisunique AS is_unique
            FROM pg_index i
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = idx.relnamespace
            CROSS JOIN LATERAL
                 unnest(i.indcollation::oid[]) WITH ORDINALITY AS key(collation_oid, position)
            JOIN pg_collation coll ON coll.oid = key.collation_oid
            WHERE n.nspname = :schema AND idx.relname = :index
            GROUP BY i.indexrelid, i.indkey, i.indisunique
        """),
        {"schema": schema, "index": COMPOSITE_MATCH_INDEX},
    ).one()
    return IndexState(
        oid=int(row.oid),
        key_attributes=str(row.key_attributes),
        collations=tuple(str(value) for value in row.collations),
        is_unique=bool(row.is_unique),
    )


def _database_revision(conn: Connection, dq_schema: str) -> str:
    return str(
        conn.execute(text(f'SELECT version_num FROM "{dq_schema}".alembic_version')).scalar_one()
    )


def _find_index_plan(node: object) -> dict[str, object] | None:
    if isinstance(node, dict):
        if node.get("Index Name") == COMPOSITE_MATCH_INDEX:
            return node
        for value in node.values():
            found = _find_index_plan(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_index_plan(value)
            if found is not None:
                return found
    return None


@pytest.fixture
def upgrade_case(test_database_url: str) -> Iterator[UpgradeCase]:
    """Create a lock-protected canonical schema and leave it at revision 0010."""
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
        command.upgrade(config, "0010")
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


def test_upgrade_0010_to_0011_replaces_raw_index_with_normalized_nonunique_index(
    upgrade_case: UpgradeCase,
) -> None:
    with upgrade_case.engine.connect() as conn:
        assert _database_revision(conn, upgrade_case.dq_schema) == "0010"
        raw = _index_state(conn, upgrade_case.commercial_schema)

    raw_keys = raw.key_attributes.split()
    assert len(raw_keys) == 4
    assert sum(key != "0" for key in raw_keys) == 3, (
        "revision 0010 must start with three raw column keys and one expression key"
    )
    assert not raw.is_unique

    revision = ScriptDirectory.from_config(upgrade_case.config).get_revision("0011")
    assert revision is not None
    assert revision.down_revision == "0010"
    command.upgrade(upgrade_case.config, "0011")

    statement = text(
        f'''EXPLAIN (FORMAT JSON, COSTS OFF)
            SELECT hcp_id
            FROM "{upgrade_case.commercial_schema}".hcp
            WHERE {_NORMALIZED_LAST_NAME} = 'oneil'
              AND {_NORMALIZED_FIRST_INITIAL} = 'j'
              AND {_NORMALIZED_POSTAL_CODE} = '02139'
              AND {_NORMALIZED_LICENCE_STATE} = 'ma' '''
    )
    with upgrade_case.engine.connect() as conn, conn.begin():
        assert _database_revision(conn, upgrade_case.dq_schema) == "0011"
        normalized = _index_state(conn, upgrade_case.commercial_schema)
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan: object = conn.execute(statement).scalar_one()

    assert normalized.oid != raw.oid, "0011 must replace, not rename, the raw index"
    assert normalized.key_attributes == "0 0 0 0", "all four keys must be expressions"
    assert normalized.collations == ("C", "C", "C", "C")
    assert not normalized.is_unique, "duplicate composite values are defects, not rejected writes"
    index_plan = _find_index_plan(plan)
    assert index_plan is not None, (
        "the replacement index cannot serve the exact T085 normalization expressions"
    )
    index_condition = str(index_plan.get("Index Cond", ""))
    for expected in ("oneil", "j", "02139", "ma"):
        assert expected in index_condition, (
            f"the planner did not use the {expected!r} normalized expression as an index key: "
            f"{json.dumps(index_plan, sort_keys=True)}"
        )
