"""Remove the obsolete persistent test-session registry (T077).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-01

Test-session liveness is now represented by session advisory locks. A crashed process releases its
database connection and lock automatically, unlike a persistent row that could remain live forever.
The public table never belongs in a prefixed test migration, so prefixed upgrades are a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import is_prefixed

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if is_prefixed():
        return
    op.get_bind().execute(text("DROP TABLE IF EXISTS public.dq_test_session"))


def downgrade() -> None:
    """No-op: persistent liveness state must not be reintroduced."""
