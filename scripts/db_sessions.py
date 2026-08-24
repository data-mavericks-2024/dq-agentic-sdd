"""List backends and blocking locks. Diagnostic; prints no secret.

    uv run python scripts/db_sessions.py            # list
    uv run python scripts/db_sessions.py --kill-idle # terminate idle-in-transaction backends

``--kill-idle`` terminates only backends that are ``idle in transaction`` and are not this
session. Those are the ones left behind by a migration run that died mid-transaction; each holds
whatever catalog locks it had taken, which is enough to make an unrelated ``CREATE FUNCTION`` wait
until ``statement_timeout`` fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, text

from dq.config.settings import as_psycopg_url, load_dotenv, require

ACTIVITY = """
SELECT pid,
       usename,
       state,
       to_char(now() - state_change, 'HH24:MI:SS')  AS in_state,
       coalesce(wait_event_type, '-')               AS wait_type,
       left(regexp_replace(coalesce(query, ''), '\\s+', ' ', 'g'), 70) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY state_change
"""

BLOCKERS = """
SELECT blocked.pid            AS blocked_pid,
       blocking.pid           AS blocking_pid,
       blocking.state         AS blocking_state,
       left(regexp_replace(coalesce(blocking.query, ''), '\\s+', ' ', 'g'), 60) AS blocking_query
FROM pg_stat_activity blocked
JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true
JOIN pg_stat_activity blocking ON blocking.pid = b.pid
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
"""


def main() -> int:
    kill_idle = "--kill-idle" in sys.argv
    load_dotenv()
    engine = create_engine(as_psycopg_url(require("SUPABASE_DB_STATEFUL_URL")), future=True)

    with engine.connect() as conn:
        print("--- backends -------------------------------------------------------")
        rows = conn.execute(text(ACTIVITY)).all()
        for r in rows:
            # Every column here can be NULL for a background worker, so nothing is formatted
            # before it has been coerced to a string.
            print(
                f"  pid={r.pid!s:<8} {r.usename or '-'!s:<22} "
                f"{r.state or '-'!s:<24} {r.in_state or '-'!s:<10} "
                f"wait={r.wait_type or '-'!s:<10} {r.query or ''}"
            )
        if not rows:
            print("  (none)")

        print("\n--- blocking chains ------------------------------------------------")
        blocks = conn.execute(text(BLOCKERS)).all()
        for b in blocks:
            print(
                f"  {b.blocked_pid} blocked by {b.blocking_pid} "
                f"({b.blocking_state}): {b.blocking_query}"
            )
        if not blocks:
            print("  (none)")

        if kill_idle:
            print("\n--- terminating idle-in-transaction backends -----------------------")
            killed = conn.execute(
                text(
                    "SELECT pid, pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND state IN ('idle in transaction', 'idle in transaction (aborted)')"
                )
            ).all()
            for k in killed:
                print(f"  pid={k.pid} terminated={k.ok}")
            if not killed:
                print("  (none to terminate)")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
