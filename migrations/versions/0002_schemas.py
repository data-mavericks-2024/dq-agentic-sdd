"""Create the five schemas (T013).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24

``commercial`` and ``dq`` hold this feature's tables. ``workflow``, ``audit``, and ``sandbox`` are
created empty so later features add tables rather than schemas — and, more usefully, so migration
0005's ``ALTER DEFAULT PRIVILEGES`` has somewhere to attach. Pre-creating an empty schema achieves
nothing on its own; it is the default privileges hanging off it that make a table added in Feature
3 inherit the right grants without anyone remembering to issue them.

Also installs ``btree_gist``, which the ``territory_alignment`` lookup index needs: a GiST index
spanning a smallint, a text, and a daterange has no operator class for the first two without it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, text

from dq.config.settings import Role
from dq.db.migration_support import is_prefixed, phys, role_exists
from dq.db.schemas import ALL_SCHEMAS, Schema

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _extension_schema(conn: Connection) -> str:
    """Where to install ``btree_gist``.

    Supabase keeps extensions in an ``extensions`` schema already on every role's ``search_path``.
    Falling back to ``public`` covers a plain PostgreSQL cluster.
    """
    found = conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'")).scalar()
    return "extensions" if found else "public"


def upgrade() -> None:
    conn = op.get_bind()

    owner = Role.MIGRATE.value if role_exists(conn, Role.MIGRATE) else None

    for schema in ALL_SCHEMAS:
        # `name` comes from dq.db.schemas.physical, which rejects anything that is not a bare
        # lower-case identifier before it can reach a DDL string.
        name = phys(schema)
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{name}"'))
        if owner:
            # IF NOT EXISTS is a no-op for the dq schema, which env.py pre-created to hold
            # alembic_version — so ownership is set explicitly rather than assumed from creation.
            conn.execute(text(f'ALTER SCHEMA "{name}" OWNER TO {owner}'))

    if owner:
        # alembic_version was created by the superuser on the first upgrade, before dq_migrate
        # existed. Once DQ_MIGRATE_URL is filled, Alembic connects *as* dq_migrate and must be able
        # to update it. Without this transfer the second upgrade fails on permission denied — after
        # the schema work has already been attempted.
        version_schema = phys(Schema.DQ)
        exists = conn.execute(
            text("SELECT 1 FROM pg_tables WHERE schemaname = :s AND tablename = 'alembic_version'"),
            {"s": version_schema},
        ).scalar()
        if exists:
            conn.execute(text(f'ALTER TABLE "{version_schema}".alembic_version OWNER TO {owner}'))

    if not is_prefixed():
        ext_schema = _extension_schema(conn)
        conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA "{ext_schema}"'))


def downgrade() -> None:
    conn = op.get_bind()
    for schema in reversed(ALL_SCHEMAS):
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{phys(schema)}" CASCADE'))
