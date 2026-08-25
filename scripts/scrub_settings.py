"""Remove allowlist rules that embed a secret, from `.claude/settings.json`.

Claude Code appends approved commands to the project allowlist, and that file **is** committed. A
command whose text contains a password — a grep pattern written to hunt for one, most likely — puts
that password into version control by a route nobody is watching.

Run after any session where a secret was typed into a command:

    uv run python scripts/scrub_settings.py <fragment> [<fragment> ...]

Prints how many rules were dropped and never the fragment itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / ".claude" / "settings.json"


def main() -> int:
    fragments = sys.argv[1:]
    if not fragments:
        print("usage: scrub_settings.py <fragment> [<fragment> ...]")
        return 1

    if not SETTINGS.is_file():
        print(f"{SETTINGS} not found")
        return 1

    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    permissions = data.get("permissions", {})

    total_removed = 0
    for key in ("allow", "deny", "ask"):
        rules = permissions.get(key)
        if not isinstance(rules, list):
            continue
        kept = [r for r in rules if not any(f in r for f in fragments)]
        removed = len(rules) - len(kept)
        if removed:
            permissions[key] = kept
            print(f"  {key}: removed {removed}, {len(kept)} remain")
        total_removed += removed

    if total_removed:
        SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"scrubbed {total_removed} rule(s) from {SETTINGS.name}")
    else:
        print("nothing matched; file unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
