"""Test schema lifecycle and the stale-schema sweep (T027, T028).

There is one Supabase project and no Docker, so integration tests run against the same database as
development. Isolation is by schema prefix, and it has to be airtight.

Each session creates ``test_<YYYYMMDDHHMMSS>_<uuid6>_{commercial,dq,workflow,audit,sandbox}``,
applies every migration into them, and drops them on teardown.

**Roles are never created or dropped here.** They are cluster-global, so schema prefixing
structurally cannot isolate them. Migration 0001 skips itself whenever a prefix is set; a prefixed
run grants the *existing* roles privileges on its own schemas and nothing else. A teardown that
dropped roles would break development outright (research.md D5).

**The sweep is the compensating control for sharing one project.** Without it, a hard-killed run
silently accumulates schemas in the working database until the 500 MB free tier fills. It reads the
timestamp out of the schema *name* because PostgreSQL records no schema creation time —
``pg_namespace`` has no such column, which is what made revision 1's version of this unimplementable
as written.
"""

from __future__ import annotations

import os
import platform
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text

from dq.config.settings import Role, Settings, as_psycopg_url, load_dotenv, load_settings
from dq.db import engine as db
from dq.db.schemas import ALL_SCHEMAS, Schema, physical

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: Schemas older than this are considered orphaned by a crashed run.
STALE_AFTER: Final[timedelta] = timedelta(hours=4)

#: `test_<14-digit UTC timestamp>_<6 hex>_<schema>`.
_SCHEMA_RE: Final[re.Pattern[str]] = re.compile(
    r"^test_(?P<ts>\d{14})_[0-9a-f]{6}_(?P<schema>commercial|dq|workflow|audit|sandbox)$"
)

#: Registry of prefixes belonging to sessions that are still running, held in `public` so it
#: survives the drop of any prefixed schema. Without it, a long `-m volume` run can have its
#: schemas swept out from under it by a session starting four hours later.
_REGISTRY_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS public.dq_test_session (
    schema_prefix text PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    host          text
)
"""


def _test_database_url() -> str:
    load_dotenv()
    raw = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw:
        raise pytest.UsageError(
            "TEST_DATABASE_URL is unset. Integration tests run against the same Supabase project "
            "as development, isolated by schema prefix — but the variable must be set explicitly "
            "so that pointing tests at a database is never accidental. Set it to the same "
            "stateful (session pooler) URL as SUPABASE_DB_STATEFUL_URL."
        )
    return as_psycopg_url(raw)


def _new_prefix() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"test_{stamp}_{uuid.uuid4().hex[:6]}_"


def _as_migrate(conn: Connection) -> None:
    """``SET ROLE dq_migrate`` so the session can drop schemas dq_migrate owns."""
    conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))


def _drop_prefixed_schemas(conn: Connection, prefix: str) -> None:
    _as_migrate(conn)
    for schema in reversed(ALL_SCHEMAS):
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{physical(schema, prefix)}" CASCADE'))
    conn.execute(text("RESET ROLE"))


def _sweep(engine: Engine) -> list[str]:
    """Drop `test_*` schemas whose embedded timestamp is older than :data:`STALE_AFTER`.

    Returns the schema names dropped, so a run that cleans up after a crash says so rather than
    doing it silently.
    """
    cutoff = datetime.now(UTC) - STALE_AFTER
    dropped: list[str] = []

    with engine.begin() as conn:
        conn.execute(text(_REGISTRY_DDL))
        live = {
            row[0] for row in conn.execute(text("SELECT schema_prefix FROM public.dq_test_session"))
        }
        candidates = [
            row[0]
            for row in conn.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test\\_%'")
            )
        ]

        stale: list[str] = []
        for name in candidates:
            match = _SCHEMA_RE.match(name)
            if not match:
                # Not ours, or not named by this harness. Leave it alone rather than guess.
                continue
            prefix = name[: name.rindex(match.group("schema"))]
            if prefix in live:
                continue
            created = datetime.strptime(match.group("ts"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            if created < cutoff:
                stale.append(name)

        if stale:
            _as_migrate(conn)
            for name in stale:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
                dropped.append(name)
            conn.execute(text("RESET ROLE"))

            # A registry row whose schemas are gone is itself stale.
            conn.execute(
                text("DELETE FROM public.dq_test_session WHERE started_at < :cutoff"),
                {"cutoff": cutoff},
            )

    return dropped


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return _test_database_url()


@pytest.fixture(scope="session")
def schema_prefix(test_database_url: str) -> Iterator[str]:
    """Create prefixed schemas, migrate into them, yield the prefix, drop them."""
    admin = create_engine(test_database_url, future=True, pool_pre_ping=True)

    dropped = _sweep(admin)
    if dropped:
        print(f"\nconftest: swept {len(dropped)} stale test schema(s): {', '.join(dropped)}")

    prefix = _new_prefix()
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.dq_test_session (schema_prefix, host) VALUES (:p, :h) "
                "ON CONFLICT (schema_prefix) DO NOTHING"
            ),
            {"p": prefix, "h": platform.node()},
        )

    previous = os.environ.get("DQ_SCHEMA_PREFIX")
    os.environ["DQ_SCHEMA_PREFIX"] = prefix
    try:
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(config, "head")
        yield prefix
    finally:
        if previous is None:
            os.environ.pop("DQ_SCHEMA_PREFIX", None)
        else:
            os.environ["DQ_SCHEMA_PREFIX"] = previous

        with admin.begin() as conn:
            _drop_prefixed_schemas(conn, prefix)
            conn.execute(
                text("DELETE FROM public.dq_test_session WHERE schema_prefix = :p"),
                {"p": prefix},
            )
        admin.dispose()


@pytest.fixture(scope="session")
def admin_engine(test_database_url: str) -> Iterator[Engine]:
    """A superuser connection, for assertions that need to see everything."""
    engine = create_engine(test_database_url, future=True, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def phys_schema(schema_prefix: str) -> dict[Schema, str]:
    """Physical schema names for this session."""
    return {schema: physical(schema, schema_prefix) for schema in ALL_SCHEMAS}


@pytest.fixture(scope="session")
def settings(schema_prefix: str) -> Settings:
    """Settings resolved *after* the prefix fixture has set ``DQ_SCHEMA_PREFIX``.

    Depending on ``schema_prefix`` is what orders these correctly: reading settings first would
    capture an empty prefix, and every connection built from it would then point at the development
    schemas rather than the isolated test ones.

    Lives in the root conftest so both the integration and volume suites can use it.
    """
    return load_settings()


@pytest.fixture(scope="session")
def findings_reader(settings: Settings) -> Iterator[Connection]:
    """A read-only connection for asserting against what was persisted.

    Connects as ``dq_readonly`` deliberately: if these assertions can be made through the
    investigation role, then Feature 3's agents can make them too, and the grant matrix is proven
    sufficient rather than assumed so.
    """
    with db.connection(settings, Role.READONLY) as conn, conn.begin():
        db.pin_session(conn, settings.schema_prefix)
        yield conn
