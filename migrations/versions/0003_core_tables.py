"""Commercial and DQ tables, with indexes (T014-T018, T020-T023).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24

The tables are created from the SQLAlchemy metadata in ``dq.domain`` rather than restated here.
Restating them would give the schema two definitions that drift, and the domain modules are what
runtime code queries against — so they are the ones that must be right.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema

# Importing these registers every Table on the shared metadata.
from dq.domain import commercial as _commercial  # noqa: F401
from dq.domain import dq as _dq  # noqa: F401
from dq.domain import metadata

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    # The GiST index on territory_alignment needs btree_gist's operator classes for the smallint
    # and text columns. Supabase installs extensions into `extensions`; a plain cluster uses
    # `public`. Both go on the path so index creation resolves either way.
    conn.execute(
        text(
            f'SET LOCAL search_path = "{phys(Schema.COMMERCIAL)}", "{phys(Schema.DQ)}", '
            f"extensions, public"
        )
    )

    # `schema_translate_map` on the connection (migrations/env.py) turns the symbolic schema names
    # carried by these tables into the prefixed physical names as the DDL is emitted.
    metadata.create_all(bind=conn, checkfirst=False)

    reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    metadata.drop_all(bind=conn, checkfirst=True)
    reset_role(conn)
