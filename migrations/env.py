"""Alembic environment — schema-prefix aware (T009).

Resolves three things the stock template does not:

**Which connection.** A prefixed run is a test run and must use ``TEST_DATABASE_URL``. An
unprefixed run uses ``DQ_MIGRATE_URL`` when it exists, and falls back to
``SUPABASE_DB_STATEFUL_URL`` only when it does not — which is true exactly once, on the first
upgrade, before migration 0001 creates ``dq_migrate``.

**Which schema holds ``alembic_version``.** The prefixed ``dq`` schema, so two concurrent test
sessions do not share a version table and neither shares development's.

**Symbolic schema names.** ``schema_translate_map`` turns ``commercial`` into
``<prefix>commercial`` at DDL-emit time, so migrations and runtime code use the same table objects.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from dq.config.settings import Role, as_psycopg_url, load_dotenv, require
from dq.db import schemas
from dq.db.migration_support import schema_prefix
from dq.db.schemas import Schema
from dq.domain import commercial as _commercial  # noqa: F401  (registers tables)
from dq.domain import dq as _dq  # noqa: F401  (registers tables)

# `metadata` must be imported after the table modules so every Table is registered on it.
from dq.domain import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_dotenv()

PREFIX = schema_prefix()
TRANSLATE_MAP = schemas.translate_map(PREFIX)
VERSION_TABLE_SCHEMA = schemas.physical(Schema.DQ, PREFIX)


def _resolve_url() -> str:
    """Return the connection URL appropriate to this run.

    Migrations should run as ``dq_migrate``. On the very first upgrade they cannot, because
    migration 0001 is what creates that role — so this probes for it rather than guessing. The
    probe costs one extra round trip and removes a bootstrap step that is otherwise easy to get
    wrong in a way that only shows up as an authentication failure mid-upgrade.
    """
    if PREFIX:
        # A prefixed run is a test run. It must never silently fall back to the development
        # connection, so this is `require`, not a lookup with a default.
        return as_psycopg_url(require("TEST_DATABASE_URL"))

    superuser_url = as_psycopg_url(require("SUPABASE_DB_STATEFUL_URL"))
    migrate_url = os.environ.get("DQ_MIGRATE_URL", "").strip()
    if not migrate_url:
        return superuser_url

    probe = create_engine(superuser_url, future=True, pool_pre_ping=True)
    try:
        with probe.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": Role.MIGRATE.value}
            ).scalar()
    finally:
        probe.dispose()

    if exists:
        return as_psycopg_url(migrate_url)

    print(
        f"env.py: {Role.MIGRATE.value} does not exist yet — connecting as the superuser so "
        f"migration 0001 can create it. Later upgrades will use DQ_MIGRATE_URL."
    )
    return superuser_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_resolve_url(),
        target_metadata=metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema=VERSION_TABLE_SCHEMA,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations."""
    engine = create_engine(_resolve_url(), future=True, pool_pre_ping=True)

    with engine.connect() as connection:
        # Chicken-and-egg: Alembic writes `alembic_version` into VERSION_TABLE_SCHEMA but will not
        # create that schema, and the migration that creates it cannot run before the version
        # table is readable. Creating it here — and only it — is the smallest resolution.
        # Migration 0002 re-creates it with IF NOT EXISTS, so the schema still has a migration of
        # record and "Alembic owns the schema" stays true.
        #
        # Checked rather than issued unconditionally: `CREATE SCHEMA IF NOT EXISTS` needs CREATE on
        # the database even when the schema already exists, and `dq_migrate` — which every upgrade
        # after the first connects as — does not hold it until migration 0005 grants it.
        already = connection.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :n"), {"n": VERSION_TABLE_SCHEMA}
        ).scalar()
        if not already:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{VERSION_TABLE_SCHEMA}"'))

        # Unconditional, and load-bearing. The probe above opens an implicit transaction whether
        # or not it creates anything. Leaving it open makes Alembic's own `begin_transaction()`
        # nest inside it rather than own it, so nothing it does is ever committed — `engine.
        # connect()` rolls the lot back on exit and the upgrade reports success having applied
        # nothing. Committing here hands Alembic a clean connection.
        connection.commit()

        connection = connection.execution_options(schema_translate_map=TRANSLATE_MAP)

        context.configure(
            connection=connection,
            target_metadata=metadata,
            include_schemas=True,
            version_table_schema=VERSION_TABLE_SCHEMA,
            # One transaction per migration, so a failure part-way leaves earlier migrations
            # applied and recorded rather than rolling the whole upgrade back. With role creation
            # in migration 0001, an all-or-nothing upgrade would repeatedly re-attempt CREATE ROLE.
            transaction_per_migration=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


# Exported for migrations that need the role name without importing settings.
MIGRATE_ROLE = Role.MIGRATE.value

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
