"""Let `dq_author` retire a feed expectation (T055b, FR-021c).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-26

Retiring a feed is end-dating its ``active`` range — an UPDATE, and ``dq_author`` held INSERT and
SELECT only. The gap went unnoticed because nothing had yet tried to perform the operation; FR-021c
described the *shape* of retirement without anyone exercising it.

The grant is column-scoped to ``active``, matching how ``dq_author`` already holds
``UPDATE (is_active) ON rule``. Both express the same idea: the role that authors a declaration may
withdraw it, and may not rewrite what the declaration said. Widening this to a table-level UPDATE
would let the cadence or the delivery window be edited under findings already raised against them,
which is the same mistake as making a rule version mutable.

Note what is deliberately *not* granted: DELETE. A retired expectation stays readable, so a finding
raised before the end date can still be explained by the declaration that produced it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    dq = phys(Schema.DQ)
    conn.execute(text(f'GRANT UPDATE (active) ON "{dq}".feed_expectation TO dq_author'))
    reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    dq = phys(Schema.DQ)
    conn.execute(text(f'REVOKE UPDATE (active) ON "{dq}".feed_expectation FROM dq_author'))
    reset_role(conn)
