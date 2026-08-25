"""Reject `:name::type` at registration (rule 8b).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25

Found by running the engine, not by reading it. Two of the nine shipped rules were written with a
postfix cast on a bound parameter, passed every one of the eight validation rules, registered
cleanly — and then failed at execution with a syntax error, because the driver declines to bind
``:name`` when a colon follows it. That is how it distinguishes a cast from a parameter marker, so
the marker survived into the statement.

The engine handled it correctly: both rules were recorded ``ERRORED`` and the run closed
``COMPLETED_WITH_ERRORS``. But the outcome is a rule that silently contributes nothing for an entire
scope, which is exactly the failure a steward is least likely to notice — the numbers simply go
quiet. Rejecting it at registration turns a recurring runtime surprise into a one-off authoring
error.

Re-applies :mod:`dq.db.sql_objects`; changes no data and adds no object.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema
from dq.db.sql_objects import define_all

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    define_all(conn, commercial=phys(Schema.COMMERCIAL), dq=phys(Schema.DQ))
    reset_role(conn)


def downgrade() -> None:
    """No-op — see migration 0006's downgrade for why a validator is not rolled back."""
