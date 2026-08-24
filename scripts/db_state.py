"""Print what currently exists in the database. Diagnostic only; prints no secret.

uv run python scripts/db_state.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, text

from dq.config.settings import as_psycopg_url, load_dotenv, redact, require

QUERIES: list[tuple[str, str]] = [
    ("current_user", "SELECT current_user"),
    ("version", "SELECT substring(version() from 'PostgreSQL [0-9.]+')"),
    (
        "dq_* roles",
        r"SELECT coalesce(string_agg(rolname, ', ' ORDER BY rolname), '(none)') "
        r"FROM pg_roles WHERE rolname LIKE 'dq\_%'",
    ),
    (
        "schemas",
        "SELECT coalesce(string_agg(nspname, ', ' ORDER BY nspname), '(none)') "
        "FROM pg_namespace WHERE nspname IN "
        "('commercial','dq','workflow','audit','sandbox')",
    ),
    (
        "schema owners",
        "SELECT coalesce(string_agg(nspname || '=' || pg_get_userbyid(nspowner), ', '), '(none)') "
        "FROM pg_namespace WHERE nspname IN ('commercial','dq','workflow','audit','sandbox')",
    ),
    (
        "tables",
        "SELECT coalesce(count(*)::text, '0') FROM pg_tables "
        "WHERE schemaname IN ('commercial','dq')",
    ),
    (
        "alembic version",
        "SELECT coalesce((SELECT version_num FROM dq.alembic_version LIMIT 1), '(none)')",
    ),
    (
        "triggers",
        "SELECT coalesce(count(*)::text,'0') FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND n.nspname IN ('commercial','dq')",
    ),
    (
        "dq functions",
        "SELECT coalesce(string_agg(p.proname, ', ' ORDER BY p.proname), '(none)') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'dq'",
    ),
    (
        "btree_gist",
        "SELECT coalesce((SELECT extversion FROM pg_extension WHERE extname='btree_gist'), '(absent)')",
    ),
    (
        "dq_migrate CREATE on db",
        "SELECT has_database_privilege('dq_migrate', current_database(), 'CREATE')::text",
    ),
]


def main() -> int:
    load_dotenv()
    url = as_psycopg_url(require("SUPABASE_DB_STATEFUL_URL"))
    print(f"connection: {redact(url)}\n")

    engine = create_engine(url, future=True, pool_pre_ping=True)
    with engine.connect() as conn:
        for label, sql in QUERIES:
            try:
                value = conn.execute(text(sql)).scalar()
            except Exception as exc:
                conn.rollback()
                value = f"ERROR: {type(exc).__name__}"
                _ = exc
            print(f"{label:26} {value}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
