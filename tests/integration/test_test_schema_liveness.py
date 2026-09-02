"""Hosted-database proof that the stale sweep preserves live sessions (T077)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

from dq.config.settings import Role
from dq.db.test_isolation import advisory_lock_key, sweep


def _stale_prefix(suffix: str) -> str:
    stamp = (datetime.now(UTC) - timedelta(hours=5)).strftime("%Y%m%d%H%M%S")
    return f"test_{stamp}_{suffix}_"


def _schema_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": name}
            ).scalar()
        )


def test_sweep_preserves_live_prefix_then_removes_it_after_disconnect(
    admin_engine: Engine, schema_prefix: str
) -> None:
    _ = schema_prefix
    prefix = _stale_prefix("a11cea")
    schema = f"{prefix}commercial"

    with admin_engine.connect() as owner:
        owner.execute(text(f'CREATE SCHEMA "{schema}"'))
        owner.execute(text(f'ALTER SCHEMA "{schema}" OWNER TO {Role.MIGRATE.value}'))
        owner.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": advisory_lock_key(prefix)}
        )
        owner.commit()

        assert schema not in sweep(admin_engine)
        assert _schema_exists(admin_engine, schema)
        # Connection.close() normally returns the physical session to SQLAlchemy's pool, which
        # would keep a session advisory lock alive. Invalidating closes that physical connection
        # and models process termination.
        owner.invalidate()

    dropped = sweep(admin_engine)

    assert schema in dropped
    assert not _schema_exists(admin_engine, schema)
