"""Point TEST_DATABASE_URL at the same stateful connection as development.

    uv run python scripts/fix_test_url.py

There is one Supabase project, so integration tests run against the same database as development
and isolate themselves by schema prefix (CLAUDE.md § Testing). ``TEST_DATABASE_URL`` therefore
carries the same value as ``SUPABASE_DB_STATEFUL_URL``, verbatim — the session pooler, because the
direct endpoint is IPv6-only on free tier and this network is IPv4-only.

Copies the raw line rather than the resolved one, so a ``[YOUR-PASSWORD]`` placeholder stays a
placeholder and the password continues to live only in ``SUPABASE_DB_PASSWORD``.

Prints no secret. Backs up ``.env`` first.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"

_ASSIGN = re.compile(r"^\s*([A-Z0-9_]+)\s*=(.*)$")


def main() -> int:
    if not ENV_PATH.is_file():
        print("ERROR: .env not found.")
        return 1

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    stateful_raw: str | None = None
    for line in lines:
        match = _ASSIGN.match(line)
        if match and match.group(1) == "SUPABASE_DB_STATEFUL_URL":
            stateful_raw = match.group(2)
            break

    if stateful_raw is None or not stateful_raw.strip():
        print("ERROR: SUPABASE_DB_STATEFUL_URL is not set in .env.")
        return 1

    shutil.copy2(ENV_PATH, ENV_PATH.parent / ".env.bak")

    replaced = False
    for i, line in enumerate(lines):
        match = _ASSIGN.match(line)
        if match and match.group(1) == "TEST_DATABASE_URL":
            old = match.group(2).strip()
            lines[i] = f"TEST_DATABASE_URL={stateful_raw}"
            replaced = True
            print(f"TEST_DATABASE_URL: replaced (previous value started {old[:28]!r})")
            break

    if not replaced:
        lines.append(f"TEST_DATABASE_URL={stateful_raw}")
        print("TEST_DATABASE_URL: appended")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Backed up to .env.bak. Value not shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
