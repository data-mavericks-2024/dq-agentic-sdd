"""Connectivity and capability probe for the Supabase project.

Answers the two questions that block Feature 1 planning:
  1. Which connection mode works from this network (direct vs session pooler)?
  2. Does this Supabase plan permit CREATE ROLE? Constitution principle 6 depends on it.

Run:  uv run --with "psycopg[binary]" python scripts/check_db.py

Reads .env directly and never prints the password.

If your database password contains any of  @ / : # ? % [ ]  it MUST be
percent-encoded inside the URL, or put it in SUPABASE_DB_PASSWORD instead and
leave the [YOUR-PASSWORD] placeholder in the URL — this script will substitute
it correctly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import psycopg

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
URL_VAR = "SUPABASE_DB_STATEFUL_URL"
PW_VAR = "SUPABASE_DB_PASSWORD"

# Placeholders the Supabase dashboard emits, in the forms people actually paste.
PLACEHOLDER = re.compile(r"\[YOUR-PASSWORD\]|\[YOUR_PASSWORD\]|<password>|YOUR-PASSWORD", re.I)


def read_env(name: str) -> str | None:
    if not ENV_FILE.exists():
        sys.exit(f"No .env found at {ENV_FILE}. Copy .env.example to .env and fill it in.")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


def clean(url: str) -> str:
    """Strip the wrappers people paste along with the URL."""
    url = url.strip().strip('"').strip("'")
    url = re.sub(r"^psql\s+", "", url)          # "psql postgresql://..."
    return url.strip('"').strip("'")


def resolve_password(url: str) -> str:
    """Substitute the dashboard placeholder with a properly encoded password."""
    if not PLACEHOLDER.search(url):
        return url
    password = read_env(PW_VAR)
    if not password:
        sys.exit(
            f"{URL_VAR} still contains the [YOUR-PASSWORD] placeholder.\n\n"
            f"Either replace it with your database password (percent-encoding any\n"
            f"of  @ / : # ? %  it contains), or add this line to .env and rerun:\n\n"
            f"    {PW_VAR}=your-actual-password\n\n"
            f"The second option is safer — this script encodes it for you."
        )
    return PLACEHOLDER.sub(quote(password, safe=""), url)


def describe(url: str) -> str:
    """Identify the connection mode. Never prints credentials."""
    parts = urlsplit(url)
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        return "UNPARSEABLE — see diagnosis below"
    port = parts.port or 5432
    if "pooler.supabase.com" in host:
        if port == 6543:
            return f"TRANSACTION pooler  {host}:{port}   <-- WRONG for stateful work"
        return f"session pooler (IPv4-safe)  {host}:{port}"
    return f"direct (IPv6-only on free tier)  {host}:{port}"


def diagnose(url: str) -> None:
    """Explain an unparseable URL without echoing the password."""
    print("The value in .env is not a valid PostgreSQL URI.\n")
    if not url.startswith(("postgresql://", "postgres://")):
        head = url[:24].split(":")[0]
        print(f"  It starts with {head!r}, not 'postgresql://'.")
        print("  Copy the full string from Connect, including the scheme.")
    if PLACEHOLDER.search(url):
        print("  It still contains the [YOUR-PASSWORD] placeholder.")
    if re.search(r"@.*@", url):
        print("  It contains more than one '@' — an unencoded '@' in the password")
        print("  splits the URI. Percent-encode it as %40, or use SUPABASE_DB_PASSWORD.")
    print("\nExpected shape (session pooler):")
    print("  postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres")


def main() -> int:
    raw = read_env(URL_VAR)
    if not raw:
        sys.exit(f"{URL_VAR} not set in .env")

    url = resolve_password(clean(raw))
    mode = describe(url)
    print(f"{URL_VAR}: {mode}\n")

    if "UNPARSEABLE" in mode:
        diagnose(url)
        return 1

    try:
        conn = psycopg.connect(url, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001 - diagnostic script, report anything
        print(f"FAILED to connect: {type(exc).__name__}: {exc}\n")
        if "pooler.supabase.com" not in url:
            print("Timed out on a direct host? The network is IPv4-only. Open Connect in")
            print("the Supabase dashboard, copy the SESSION POOLER string (port 5432 on")
            print(f"...pooler.supabase.com), and put it in {URL_VAR}.")
        else:
            print("Check the password, and that the user is 'postgres.<project-ref>'")
            print("(the session pooler requires the ref suffix on the username).")
        return 1

    with conn:
        version = conn.execute("select version()").fetchone()[0]
        print(f"connected: {version.split(' on ')[0]}")
        print(f"database:  {conn.execute('select current_database()').fetchone()[0]}")
        print(f"user:      {conn.execute('select current_user').fetchone()[0]}")

        # Prepared statements must work, or Alembic and PostgresSaver will break.
        try:
            conn.execute("select 1", prepare=True)
            print("prepared statements: OK")
        except Exception as exc:  # noqa: BLE001
            print(f"prepared statements: FAILED ({type(exc).__name__})")
            print("  You are on the transaction pooler. Switch to session pooler or direct.")

        # Advisory locks: the checkpointer and Alembic both rely on these.
        try:
            conn.execute("select pg_advisory_lock(1)")
            conn.execute("select pg_advisory_unlock(1)")
            print("advisory locks:      OK")
        except Exception as exc:  # noqa: BLE001
            print(f"advisory locks:      FAILED ({type(exc).__name__})")

        # Constitution principle 6 requires four least-privilege roles.
        try:
            conn.execute("create role dq_probe nologin")
            conn.execute("drop role dq_probe")
            print("CREATE ROLE:         OK -- principle 6 is implementable")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            print(f"CREATE ROLE:         DENIED ({type(exc).__name__}: {exc})")
            print("  Principle 6 (least-privilege roles) needs rethinking. Flag this.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
