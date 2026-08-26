"""Remove allowlist rules from `.claude/settings.json` that embed a secret.

    uv run python scripts/scrub_settings.py

**Takes no arguments, on purpose.** Claude Code appends every approved command to the project
allowlist, and that file is committed — so a command line containing a secret puts the secret into
version control by a route nobody is watching. An earlier version of this script accepted the
fragment as `argv[1]`, which meant *running the cleanup* added a fresh copy of the thing it was
cleaning up. It removed three rules and created one, forever.

So the secrets are read from `.env`, never passed in. Nothing sensitive appears on a command line,
in shell history, or in the allowlist entry this invocation itself creates.

Scrubs any rule containing a `.env` value that looks like a credential, plus a few well-known
prefixes. Prints counts and variable names, never values.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
ENV = REPO_ROOT / ".env"

#: Variables whose values are secrets worth hunting for. Connection URLs are included because the
#: password is embedded in them.
SECRET_VAR_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|_KEY|_URL)$", re.IGNORECASE)

#: Values shorter than this are too likely to appear coincidentally inside an ordinary command.
MIN_SECRET_LENGTH = 8

#: Unfilled template text. Supabase hands its connection string out containing `[YOUR-PASSWORD]`,
#: and treating that as a credential makes the literal word `PASSWORD` a secret — which then matches
#: this file, every scanner, and half the documentation. A placeholder is the *absence* of a
#: secret, so it is skipped and reported rather than matched.
PLACEHOLDER_MARKERS = ("[", "]", "<", ">", "YOUR-", "CHANGEME", "REPLACE_ME")

#: Credential formats recognisable regardless of what `.env` holds.
#:
#: Written as patterns requiring the key material, not bare prefixes. A literal ``"sk-ant-"`` would
#: match this very file and every scanner that mentions the format — a check that flags its own
#: source teaches people to ignore it.
ALWAYS_SCRUB_PATTERNS = (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),)


def _fragments(secret: str) -> set[str]:
    """The secret, plus each of its alphanumeric components long enough to be identifying.

    A password like ``Correct-horse@2026`` shows up in a command line as ``Correct-horse%402026``
    once URL-encoded, and in a hand-written grep pattern as just ``Correct``. Matching the whole
    value misses all of those. Splitting on non-alphanumerics catches the parts that carry the
    entropy, and the length floor keeps ordinary words like ``2026`` out.

    A password prefix in a committed file is still a leak — it removes most of the search space.
    """
    parts = {secret}
    for part in re.split(r"[^A-Za-z0-9]+", secret):
        if len(part) >= MIN_SECRET_LENGTH:
            parts.add(part)
    return parts


def _secrets_from_env(report_unfilled: bool = False) -> list[str]:
    """Every credential-shaped value in `.env`, plus the password inside any connection URL.

    Set ``report_unfilled`` to print variables still holding template text, which are worth knowing
    about in their own right: an unfilled placeholder is configuration that has not been done.
    """
    if not ENV.is_file():
        return []

    found: set[str] = set()
    unfilled: list[str] = []
    for raw in ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not value or len(value) < MIN_SECRET_LENGTH:
            continue
        if not SECRET_VAR_PATTERN.search(key):
            continue

        if any(marker in value.upper() for marker in PLACEHOLDER_MARKERS):
            unfilled.append(key)
            continue

        looks_like_url = "://" in value

        if looks_like_url:
            # The password lives in the userinfo; the host and path do not. Fragmenting the whole
            # URL would make `supabase` a secret and scrub every rule mentioning the pooler.
            url_password = re.match(r"^[a-z+]+://[^:/@]+:([^@]+)@", value)
            if url_password and len(url_password.group(1)) >= MIN_SECRET_LENGTH:
                found.add(value)
                found |= _fragments(url_password.group(1))
        elif key.upper().endswith("_URL"):
            # A `_URL` variable whose value is not a URL is malformed configuration, not a
            # credential. Fragmenting a bare hostname turns `supabase` and `southeast` into
            # "secrets" and the checker starts crying wolf on every rule that names the host.
            continue
        else:
            found.add(value)
            found |= _fragments(value)

    if report_unfilled and unfilled:
        print(f"  note: still holding placeholder text: {', '.join(unfilled)}")

    return sorted(found, key=len, reverse=True)


def main() -> int:
    if len(sys.argv) > 1:
        print("This script takes no arguments — see the module docstring for why.")
        return 2

    if not SETTINGS.is_file():
        print(f"{SETTINGS} not found; nothing to scrub.")
        return 0

    secrets = _secrets_from_env(report_unfilled=True)
    if not secrets:
        print("No credential-shaped values found in .env; nothing to match against.")
        return 0

    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    permissions = data.get("permissions", {})

    total = 0
    for key in ("allow", "deny", "ask"):
        rules = permissions.get(key)
        if not isinstance(rules, list):
            continue
        kept = [
            r
            for r in rules
            if not any(s in r for s in secrets)
            and not any(p.search(r) for p in ALWAYS_SCRUB_PATTERNS)
        ]
        removed = len(rules) - len(kept)
        if removed:
            permissions[key] = kept
            print(f"  {key}: removed {removed}, {len(kept)} remain")
        total += removed

    if total:
        SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Scrubbed {total} rule(s) from {SETTINGS.name}.")
    else:
        print("Clean — no allowlist rule contains a secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
