"""Immutability and predicate-determinism triggers (T019, T024, T025).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24

Three enforcement mechanisms, all in the database rather than in application code, because the
roles that write these tables can reach them directly. A check that lives only in a Python
registration path is a convention, and constitution principle I does not accept conventions.

* **T019** — ``data_batch`` and every batch member table reject UPDATE, DELETE, and TRUNCATE
  (FR-003b). Revision 1 protected only the batch header, which left the contents mutable.
* **T024** — ``rule_version`` and ``finding`` reject the same. They are the audit-bearing tables:
  a finding says what was true at a moment, and a rule version says what "failed" meant at that
  moment.
* **T025** — a ``BEFORE INSERT`` trigger on ``rule_version`` enforcing the eight validation rules
  in contracts/rule-definition.md, plus FR-016d's rejection of similarity matching.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema
from dq.db.sql_objects import define_all
from dq.domain.commercial import BATCH_MEMBER_TABLES
from dq.domain.dq import IMMUTABLE_TABLES

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The three function bodies live in `dq.db.sql_objects` rather than here. Inlining a 300-line
# PL/pgSQL body in a migration freezes it at this revision: changing it later means either editing
# an applied migration (which never reaches a database that already ran it) or pasting a second
# copy into a new one (which drifts). Migration 0006 re-applies the same module, so a fresh
# database and an existing one converge on whatever that module says today.


def _immutable_triggers(schema: str, table: str, dq_schema: str) -> list[str]:
    return [
        f'DROP TRIGGER IF EXISTS trg_{table}_immutable ON "{schema}"."{table}"',
        f'CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "{schema}"."{table}" '
        f'FOR EACH STATEMENT EXECUTE FUNCTION "{dq_schema}".reject_mutation()',
        f'DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON "{schema}"."{table}"',
        f'CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON "{schema}"."{table}" '
        f'FOR EACH STATEMENT EXECUTE FUNCTION "{dq_schema}".reject_mutation()',
    ]


def upgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    commercial = phys(Schema.COMMERCIAL)
    dq = phys(Schema.DQ)

    # T025's validator and the shared mutation-rejecting function.
    define_all(conn, commercial=commercial, dq=dq)

    # T019 — the batch header and every batch member table.
    for table in ("data_batch", *BATCH_MEMBER_TABLES):
        for stmt in _immutable_triggers(commercial, table, dq):
            conn.execute(text(stmt))

    # T024 — the audit-bearing tables.
    for table in IMMUTABLE_TABLES:
        for stmt in _immutable_triggers(dq, table, dq):
            conn.execute(text(stmt))

    # T025 — predicate determinism.
    conn.execute(text(f'DROP TRIGGER IF EXISTS trg_rule_version_validate ON "{dq}".rule_version'))
    conn.execute(
        text(
            f'CREATE TRIGGER trg_rule_version_validate BEFORE INSERT ON "{dq}".rule_version '
            f'FOR EACH ROW EXECUTE FUNCTION "{dq}".rule_version_validate()'
        )
    )

    reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    commercial = phys(Schema.COMMERCIAL)
    dq = phys(Schema.DQ)

    conn.execute(text(f'DROP TRIGGER IF EXISTS trg_rule_version_validate ON "{dq}".rule_version'))
    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".rule_version_validate()'))
    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".assert_predicate_valid(text, jsonb, text)'))

    for schema, tables in (
        (commercial, ("data_batch", *BATCH_MEMBER_TABLES)),
        (dq, IMMUTABLE_TABLES),
    ):
        for table in tables:
            conn.execute(
                text(f'DROP TRIGGER IF EXISTS trg_{table}_immutable ON "{schema}"."{table}"')
            )
            conn.execute(
                text(f'DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON "{schema}"."{table}"')
            )

    conn.execute(text(f'DROP FUNCTION IF EXISTS "{dq}".reject_mutation()'))
    reset_role(conn)
