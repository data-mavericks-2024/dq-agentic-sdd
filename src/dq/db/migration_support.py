"""Helpers shared by Alembic migrations.

Lives under ``src/`` rather than in ``migrations/`` so it is type-checked and importable by tests.

The one non-obvious thing here is :func:`assume_migrate_role`. The very first ``alembic upgrade
head`` runs as the Supabase superuser, because ``dq_migrate`` does not exist until migration 0001
creates it. Everything after that point should be owned by ``dq_migrate``, or
``ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate`` in migration 0005 attaches to a role that owns
nothing and silently grants nothing to future tables.
"""

from __future__ import annotations

import os

from sqlalchemy import Connection, text

from dq.config.settings import Role
from dq.db.schemas import Schema, assert_safe_identifier, physical


def schema_prefix() -> str:
    """Return the validated ``DQ_SCHEMA_PREFIX``, empty in development."""
    prefix = os.environ.get("DQ_SCHEMA_PREFIX", "").strip()
    if prefix:
        # Validated by round-tripping through a schema name; raises on anything unsafe.
        assert_safe_identifier(f"{prefix}dq")
    return prefix


def is_prefixed() -> bool:
    """True when this upgrade is running against isolated test schemas.

    The bootstrap migration keys off this. Roles are cluster-global, so a prefixed run that created
    or dropped them would be mutating development, not isolating from it (research.md D5).
    """
    return bool(schema_prefix())


def phys(schema: Schema) -> str:
    """Physical name of ``schema`` under the current prefix."""
    return physical(schema, schema_prefix())


def role_exists(conn: Connection, role: Role) -> bool:
    """True if ``role`` is present in the cluster."""
    found = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": role.value}
    ).scalar()
    return found is not None


def assume_migrate_role(conn: Connection) -> None:
    """``SET ROLE dq_migrate`` when possible, so created objects are owned by it.

    A no-op when the role does not exist yet (the first upgrade, before migration 0001) or when the
    current user is not a member of it. Being permissive here is correct: on a prefixed test run
    the connection is often already ``dq_migrate``, and on a fresh cluster the role is created
    moments later.
    """
    if not role_exists(conn, Role.MIGRATE):
        return
    current = conn.execute(text("SELECT current_user")).scalar_one()
    if current == Role.MIGRATE.value:
        return
    is_member = conn.execute(
        text("SELECT pg_has_role(current_user, :role, 'MEMBER')"), {"role": Role.MIGRATE.value}
    ).scalar()
    if is_member:
        conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))


def reset_role(conn: Connection) -> None:
    """Undo :func:`assume_migrate_role`."""
    conn.execute(text("RESET ROLE"))


def execute_raw(conn: Connection, sql: str) -> None:
    """Execute ``sql`` with no placeholder processing whatsoever.

    PL/pgSQL is full of ``%`` — ``RAISE`` placeholders and ``format()`` specifiers. Neither
    ``text()`` nor ``exec_driver_sql`` can carry it unchanged: SQLAlchemy escapes each ``%`` to
    ``%%`` for psycopg's pyformat paramstyle, and psycopg then rejects the ones it cannot read as
    placeholders (``only '%s', '%b', '%t' are allowed as placeholders``).

    Going to the driver cursor with no ``params`` argument skips client-side parsing entirely. The
    cursor is on the same connection, so this participates in the migration's transaction.

    Only ever called with SQL composed in this repository from validated identifiers — never with
    anything derived from user input.
    """
    driver_conn = conn.connection.driver_connection
    with driver_conn.cursor() as cur:  # type: ignore[union-attr]
        cur.execute(sql)
