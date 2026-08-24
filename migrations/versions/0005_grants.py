"""The grant matrix (T026).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24

Constitution principle VI made real. Principle II states the rule — no agent may mutate curated
commercial data — and this file is the mechanism that makes it true even when everything above the
database misbehaves. If an investigation agent physically cannot issue a write, prompt injection
into a data value cannot cause one.

``ALTER DEFAULT PRIVILEGES`` is issued per schema and per verb. Without it, pre-creating the empty
``workflow``, ``audit``, and ``sandbox`` schemas achieves nothing: a table added by a Feature 3
migration would arrive with no grants and the first agent to touch it would fail, or worse, someone
would fix it with a broad grant.

**No ``audit`` or ``workflow`` grant is issued to ``dq_readonly``, deliberately.** Those schemas
will hold persisted prompt payloads and checkpointed agent state, and ``dq_readonly`` is the role
investigation agents connect under — so an agent processing an injected instruction inside a data
value could read other investigations' payloads. Both schemas are empty in this feature, so the
decision costs nothing now and is deferred to Feature 3 where it becomes real. Recorded here so it
is not mistaken for an oversight.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from dq.config.settings import Role
from dq.db.migration_support import (
    assume_migrate_role,
    phys,
    reset_role,
    role_exists,
)
from dq.db.schemas import Schema

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    missing = [r.value for r in Role if not role_exists(conn, r)]
    if missing:
        raise RuntimeError(
            f"Cannot apply grants: roles {missing} do not exist. On an unprefixed run this means "
            f"migration 0001 did not complete. On a prefixed test run it means the bootstrap has "
            f"never been applied to this cluster — run `alembic upgrade head` once with "
            f"DQ_SCHEMA_PREFIX unset first."
        )

    # NOTE — `GRANT CREATE ON DATABASE ... TO dq_migrate` is deliberately NOT issued here.
    #
    # It would let dq_migrate create a sixth schema in a later feature. It also updates a row in
    # the cluster-wide `pg_database` catalog, and on this Supabase project that row is contended:
    # the statement blocks and dies on statement_timeout, taking the pooled connection with it.
    #
    # Nothing needs it yet. dq_migrate already owns all five schemas, so it holds full DDL inside
    # each, and migrations/env.py checks for the version-table schema rather than issuing an
    # unconditional CREATE SCHEMA IF NOT EXISTS (which needs the privilege even when the schema
    # exists). The first feature that genuinely adds a schema must run that single GRANT as the
    # Supabase owner, out of band, and should expect to retry it.
    assume_migrate_role(conn)

    commercial = phys(Schema.COMMERCIAL)
    dq = phys(Schema.DQ)
    sandbox = phys(Schema.SANDBOX)
    audit = phys(Schema.AUDIT)
    # `workflow` is intentionally not granted to anything: dq_migrate owns it and LangGraph's
    # checkpointer arrives in Feature 2, under a role chosen then.

    stmts: list[str] = [
        # -- USAGE ---------------------------------------------------------
        f'GRANT USAGE ON SCHEMA "{commercial}" TO dq_ingest, dq_engine, dq_readonly, dq_sandbox, dq_publish',
        f'GRANT USAGE ON SCHEMA "{dq}" TO dq_author, dq_engine, dq_readonly, dq_sandbox, dq_publish',
        f'GRANT USAGE ON SCHEMA "{sandbox}" TO dq_sandbox, dq_readonly',
        f'GRANT USAGE ON SCHEMA "{audit}" TO dq_ingest, dq_author, dq_engine, dq_sandbox, dq_publish',
        # dq_readonly is deliberately absent from the audit and workflow USAGE grants above.
        # -- dq_ingest: loads source data, cannot read quality metadata ----
        f'GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA "{commercial}" TO dq_ingest',
        # -- dq_author: writes the rule registry, no commercial access -----
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dq}" TO dq_author',
        f'GRANT INSERT ON "{dq}".rule, "{dq}".rule_version TO dq_author',
        f'GRANT UPDATE (is_active) ON "{dq}".rule TO dq_author',
        # -- dq_engine: reads commercial, writes findings, never commercial
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{commercial}" TO dq_engine',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dq}" TO dq_engine',
        f'GRANT INSERT ON "{dq}".finding, "{dq}".rule_run, "{dq}".rule_run_rule_version TO dq_engine',
        # Narrow UPDATE so a run can be closed out without the engine being able to rewrite the
        # scope, the watermark, or the session settings it recorded at start.
        f'GRANT UPDATE (finished_at, status, error_detail) ON "{dq}".rule_run TO dq_engine',
        f'GRANT UPDATE (outcome, finding_count, error_detail) ON "{dq}".rule_run_rule_version TO dq_engine',
        # -- dq_readonly: investigation agents ------------------------------
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{commercial}" TO dq_readonly',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dq}" TO dq_readonly',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{sandbox}" TO dq_readonly',
        # -- dq_sandbox: simulation, writes only to sandbox -----------------
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{commercial}" TO dq_sandbox',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dq}" TO dq_sandbox',
        f'GRANT ALL ON ALL TABLES IN SCHEMA "{sandbox}" TO dq_sandbox',
        f'GRANT CREATE ON SCHEMA "{sandbox}" TO dq_sandbox',
        # -- dq_publish: governed publish, post-approval only ---------------
        f'GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA "{commercial}" TO dq_publish',
        f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dq}" TO dq_publish',
        # -- audit: append-only for everything that acts ---------------------
        f'GRANT INSERT ON ALL TABLES IN SCHEMA "{audit}" TO dq_ingest, dq_author, dq_engine, dq_sandbox, dq_publish',
        # -- Defaults, per schema and per verb ------------------------------
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{commercial}" '
        f"GRANT SELECT ON TABLES TO dq_engine, dq_readonly, dq_sandbox, dq_publish, dq_ingest",
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{commercial}" '
        f"GRANT INSERT ON TABLES TO dq_ingest, dq_publish",
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{dq}" '
        f"GRANT SELECT ON TABLES TO dq_engine, dq_readonly, dq_sandbox, dq_author",
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{sandbox}" '
        f"GRANT SELECT ON TABLES TO dq_readonly",
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{sandbox}" '
        f"GRANT ALL ON TABLES TO dq_sandbox",
        f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{audit}" '
        f"GRANT INSERT ON TABLES TO dq_ingest, dq_author, dq_engine, dq_sandbox, dq_publish",
        # workflow: dq_migrate owns it and nothing else is granted. LangGraph's checkpointer
        # arrives in Feature 2 and will connect under a role chosen then.
    ]

    for stmt in stmts:
        conn.execute(text(stmt))

    reset_role(conn)


def downgrade() -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)

    roles = ", ".join(r.value for r in Role if r is not Role.MIGRATE)
    for schema in (Schema.COMMERCIAL, Schema.DQ, Schema.SANDBOX, Schema.AUDIT, Schema.WORKFLOW):
        name = phys(schema)
        conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{name}" FROM {roles}'))
        conn.execute(text(f'REVOKE ALL ON SCHEMA "{name}" FROM {roles}'))
        conn.execute(
            text(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA "{name}" '
                f"REVOKE ALL ON TABLES FROM {roles}"
            )
        )

    reset_role(conn)
