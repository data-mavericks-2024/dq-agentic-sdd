"""Bind the scope into `source_period` predicates (T047 support).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25

Adds ``scope_source_system_id``, ``scope_period_start``, and ``scope_period_end`` to the validator's
engine-bound parameter list, by re-applying :mod:`dq.db.sql_objects`.

**Why the contract needed extending.** A ``source_period`` rule has no batch to anchor to, and the
original three engine-bound parameters gave it no way to learn which source and period it was being
asked about. A feed rule would therefore have had to evaluate *every* declared expectation on every
run — and the same missing period would then re-fire under a fresh ``scope_key`` each time, which is
precisely the duplication the uniqueness key was rekeyed to prevent (research.md D4, FR-011).

The alternative was to have each feed rule declare the three names in its own ``parameters`` block
and let the engine quietly override the declared values at bind time. That works without a
migration, and it was rejected: a declared parameter whose value is ignored is a lie in the rule
definition, and the next author to read one would have no way to tell which parameters are real.

This migration changes no data and adds no object. It replaces one function body.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema
from dq.db.sql_objects import define_all

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    define_all(conn, commercial=phys(Schema.COMMERCIAL), dq=phys(Schema.DQ))

    # A feed expectation declares what data *should* arrive. That is authored configuration of the
    # same kind as a rule, so it belongs to dq_author — the role that already owns the registry —
    # rather than to dq_ingest, which loads data and is deliberately blind to quality metadata.
    conn.execute(text(f'GRANT INSERT ON "{phys(Schema.DQ)}".feed_expectation TO dq_author'))

    reset_role(conn)


def downgrade() -> None:
    """No-op.

    Downgrading would mean restoring a *narrower* engine-bound list, which cannot be expressed by
    re-applying the current module — and any rule registered since would then fail rule 8 on its
    next re-registration. The forward-only choice is deliberate: this migration removes no
    capability, so there is nothing a rollback recovers.
    """
