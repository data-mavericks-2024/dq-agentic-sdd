"""Persist explicit historical replay lineage (T081).

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "rule_run"
_COLUMN = "replay_of_rule_run_id"
_FOREIGN_KEY = "fk_rule_run_replay_of_rule_run_id"


def _has_column(conn: sa.Connection, schema: str) -> bool:
    return any(
        column["name"] == _COLUMN for column in inspect(conn).get_columns(_TABLE, schema=schema)
    )


def _has_foreign_key(conn: sa.Connection, schema: str) -> bool:
    return any(
        foreign_key.get("constrained_columns") == [_COLUMN]
        for foreign_key in inspect(conn).get_foreign_keys(_TABLE, schema=schema)
    )


def upgrade() -> None:
    conn = op.get_bind()
    schema = phys(Schema.DQ)
    assume_migrate_role(conn)
    try:
        # A clean install reaches 0013 after migration 0003 has imported current metadata, while
        # an existing database reaches it with the historical 0012 shape. Support both paths.
        if not _has_column(conn, schema):
            op.add_column(
                _TABLE,
                sa.Column(_COLUMN, sa.BigInteger(), nullable=True),
                schema=schema,
            )
        if not _has_foreign_key(conn, schema):
            op.create_foreign_key(
                _FOREIGN_KEY,
                _TABLE,
                _TABLE,
                [_COLUMN],
                ["rule_run_id"],
                source_schema=schema,
                referent_schema=schema,
                ondelete="RESTRICT",
            )
    finally:
        reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    schema = phys(Schema.DQ)
    assume_migrate_role(conn)
    try:
        if _has_foreign_key(conn, schema):
            op.drop_constraint(_FOREIGN_KEY, _TABLE, schema=schema, type_="foreignkey")
        if _has_column(conn, schema):
            op.drop_column(_TABLE, _COLUMN, schema=schema)
    finally:
        reset_role(conn)
