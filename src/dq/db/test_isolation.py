"""Hosted-test schema liveness using PostgreSQL session advisory locks."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import Connection, Engine, text

from dq.config.settings import Role
from dq.db.schemas import ALL_SCHEMAS, physical

STALE_AFTER: Final[timedelta] = timedelta(hours=4)

_SCHEMA_RE: Final[re.Pattern[str]] = re.compile(
    r"^test_(?P<ts>\d{14})_[0-9a-f]{6}_(?P<schema>commercial|dq|workflow|audit|sandbox)$"
)


def advisory_lock_key(prefix: str) -> int:
    """Return a stable signed 64-bit advisory-lock key for ``prefix``."""
    digest = hashlib.blake2b(
        prefix.encode("ascii"), digest_size=8, person=b"dq-tests"
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def acquire_session_lock(conn: Connection, prefix: str) -> None:
    """Block until this session owns the liveness lock for ``prefix``."""
    conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": advisory_lock_key(prefix)})
    conn.commit()


def release_session_lock(conn: Connection, prefix: str) -> None:
    """Release the liveness lock held by this database session."""
    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": advisory_lock_key(prefix)})
    conn.commit()


def _drop_prefix(conn: Connection, prefix: str) -> None:
    conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
    try:
        for schema in reversed(ALL_SCHEMAS):
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{physical(schema, prefix)}" CASCADE'))
    finally:
        conn.execute(text("RESET ROLE"))


def sweep(
    engine: Engine,
    *,
    stale_after: timedelta = STALE_AFTER,
    now: datetime | None = None,
) -> list[str]:
    """Drop stale prefixed schemas only while holding their liveness lock."""
    cutoff = (now or datetime.now(UTC)) - stale_after
    dropped: list[str] = []

    with engine.begin() as conn:
        candidates = [
            str(row[0])
            for row in conn.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'test\\_%'")
            )
        ]

        stale_by_prefix: dict[str, list[str]] = {}
        for name in candidates:
            match = _SCHEMA_RE.match(name)
            if match is None:
                continue
            created = datetime.strptime(match.group("ts"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            if created >= cutoff:
                continue
            prefix = name[: name.rindex(match.group("schema"))]
            stale_by_prefix.setdefault(prefix, []).append(name)

        for prefix, names in stale_by_prefix.items():
            key = advisory_lock_key(prefix)
            acquired = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one()
            )
            if not acquired:
                continue
            try:
                _drop_prefix(conn, prefix)
                dropped.extend(names)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

    return dropped
