"""Report which environment variables are set, and the shape of each URL.

    uv run python scripts/env_check.py

Prints no password and no full connection string — for URL variables it shows only scheme, user,
host, port, and database, which is what you need to spot a wrong endpoint or a placeholder left in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dq.config.settings import ROLE_PASSWORD_VAR, ROLE_URL_VAR, Role, load_dotenv

URL_VARS = [
    "SUPABASE_DB_STATEFUL_URL",
    "SUPABASE_DB_POOLED_URL",
    "TEST_DATABASE_URL",
    *[ROLE_URL_VAR[r] for r in Role],
]

PLAIN_VARS = [
    "SUPABASE_PROJECT_REF",
    "TEST_SCHEMA_PREFIX",
    "DQ_SCHEMA_PREFIX",
]

SECRET_VARS = [
    "ANTHROPIC_API_KEY",
    "SUPABASE_DB_PASSWORD",
    *[ROLE_PASSWORD_VAR[r] for r in Role],
]


def describe_url(raw: str) -> str:
    """Scheme, user, host, port, database — never the password."""
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return f"UNPARSEABLE ({exc})"
    if not parts.scheme:
        return f"NOT A URL (starts {raw[:24]!r})"
    if not parts.hostname:
        return "UNPARSEABLE (no host)"
    user = parts.username or "(no user)"
    port = parts.port or "(default)"
    return f"{parts.scheme}://{user}@{parts.hostname}:{port}{parts.path}"


def main() -> int:
    load_dotenv()

    print("--- URLs ---------------------------------------------------------")
    for name in URL_VARS:
        raw = os.environ.get(name, "").strip()
        print(f"  {name:26} {describe_url(raw) if raw else 'UNSET'}")

    print("\n--- plain values -------------------------------------------------")
    for name in PLAIN_VARS:
        raw = os.environ.get(name, "").strip()
        print(f"  {name:26} {raw if raw else 'UNSET (empty)'}")

    print("\n--- secrets (presence and length only) ---------------------------")
    for name in SECRET_VARS:
        raw = os.environ.get(name, "").strip()
        print(f"  {name:26} {'SET, ' + str(len(raw)) + ' chars' if raw else 'UNSET'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
