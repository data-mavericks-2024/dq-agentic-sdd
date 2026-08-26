"""Rule 7 requires the as-of parameters to be *applied*, not merely present (T064).

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-26

The earlier check asked whether ``:as_of_date`` and ``:reference_watermark`` appeared anywhere in
the predicate. A rule could satisfy that by mentioning one in an unrelated clause while still
joining master data unbounded — which is research.md R2 almost exactly: *the predicate still returns
plausible results, still passes the determinism test, and silently breaks historical reproducibility*.

Each parameter must now be compared against the versioning column it exists to constrain:
``valid_from <= :as_of_date`` and ``batch_id <= :reference_watermark``.

Still a lexical check, and still weaker than a parse-tree proof — it cannot tell that the comparison
sits inside the *same* join as the master reference. It closes the gap that mattered, which is a
parameter bound and then ignored.

Re-applies :mod:`dq.db.sql_objects`; changes no data and adds no object.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema
from dq.db.sql_objects import define_all

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    define_all(conn, commercial=phys(Schema.COMMERCIAL), dq=phys(Schema.DQ))
    reset_role(conn)


def downgrade() -> None:
    """No-op — see migration 0006's downgrade for why a validator is not rolled back."""
