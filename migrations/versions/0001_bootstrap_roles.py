"""Bootstrap the seven database roles (T012).

Revision ID: 0001
Revises:
Create Date: 2026-08-24

**Skipped entirely when ``DQ_SCHEMA_PREFIX`` is non-empty.** Roles are cluster-global, so schema
prefixing structurally cannot isolate them. A prefixed test run that created them would collide
with development; one that dropped them on teardown would break development outright. Test
sessions grant the *existing* roles privileges on their own prefixed schemas and nothing more
(research.md D5).

This is the one migration that does not run as ``dq_migrate`` — it creates that role.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.config.settings import ROLE_PASSWORD_VAR, Role
from dq.db.migration_support import is_prefixed, role_exists

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Constitution v1.1.0 principle VI, in order. The list is exhaustive: an eighth role fails
#: tests/integration/test_role_conformance.py until the constitution is amended.
ROLES: tuple[Role, ...] = (
    Role.MIGRATE,
    Role.INGEST,
    Role.AUTHOR,
    Role.ENGINE,
    Role.READONLY,
    Role.SANDBOX,
    Role.PUBLISH,
)


def _password_for(role: Role) -> str:
    var = ROLE_PASSWORD_VAR[role]
    value = os.environ.get(var, "").strip()
    if not value:
        raise RuntimeError(
            f"{var} is unset. Generate all seven with "
            f"`uv run python scripts/gen_role_passwords.py`, which writes them into .env and "
            f"prints nothing. Roles are created with a password once and only once."
        )
    if len(value) < 24:
        raise RuntimeError(f"{var} is shorter than 24 characters. Use the generator.")
    return value


def upgrade() -> None:
    if is_prefixed():
        print("0001: DQ_SCHEMA_PREFIX is set — skipping role creation (roles are cluster-global).")
        return

    conn = op.get_bind()

    for role in ROLES:
        password = _password_for(role)
        # DDL cannot be parameterised, so the password is escaped by PostgreSQL's own
        # `quote_literal` — bound as a parameter on the way in, returned already quoted — rather
        # than by hand-rolled string escaping here. The role name is not caller-controlled: it
        # comes from the Role enum above.
        quoted = conn.execute(text("SELECT quote_literal(:pw)"), {"pw": password}).scalar_one()

        if role_exists(conn, role):
            # Idempotent re-apply: make .env authoritative rather than leaving a role whose
            # password no longer matches any connection string.
            conn.execute(text(f"ALTER ROLE {role.value} WITH LOGIN NOINHERIT PASSWORD {quoted}"))
        else:
            conn.execute(text(f"CREATE ROLE {role.value} LOGIN NOINHERIT PASSWORD {quoted}"))

    # NOINHERIT on every role, and no role granted membership in another. Asserted by
    # tests/integration/test_role_conformance.py rather than trusted.
    for role in ROLES:
        conn.execute(text(f"ALTER ROLE {role.value} NOINHERIT"))

    # Supabase's `postgres` user is not a superuser; it must be a member of each new role to grant
    # their privileges later. NOINHERIT above means membership confers nothing without SET ROLE,
    # so this does not weaken the separation between the seven.
    grantee = conn.execute(text("SELECT current_user")).scalar_one()
    for role in ROLES:
        conn.execute(text(f'GRANT {role.value} TO "{grantee}"'))


def downgrade() -> None:
    """Drop the seven roles.

    Destructive and cluster-global: any object they own goes with them. Guarded to unprefixed runs
    for the same reason `upgrade` is.
    """
    if is_prefixed():
        return

    conn = op.get_bind()
    for role in reversed(ROLES):
        if not role_exists(conn, role):
            continue
        conn.execute(text(f"DROP OWNED BY {role.value} CASCADE"))
        conn.execute(text(f"DROP ROLE {role.value}"))
