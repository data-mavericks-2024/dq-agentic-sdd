"""Enforce reference watermarks on every historical delivery read (T080).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-10

Rule 7 originally protected version-chain master tables only. ``territory_alignment`` and
``data_batch`` can also receive later batches whose business dates fall inside a historical scope,
so unbounded reads of either table silently changed earlier results. Reapply the replaceable SQL
objects so existing databases receive alias-specific delivery-watermark validation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema
from dq.db.sql_objects import define_all

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    define_all(conn, commercial=phys(Schema.COMMERCIAL), dq=phys(Schema.DQ))
    reset_role(conn)


def downgrade() -> None:
    """No-op: weakening an installed predicate validator is not a safe rollback."""
