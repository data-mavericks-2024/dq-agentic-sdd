"""Fail if anything staged for commit contains a credential.

    uv run python scripts/check_staged_for_secrets.py

Reads the secrets from `.env` rather than taking them as arguments, for the same reason
:mod:`scrub_settings` does: a secret on a command line ends up in shell history and in the Claude
Code allowlist, and the allowlist is committed. A leak-checker that leaks is not a good trade.

Reports file names and match counts. **Never prints the matched text** — a scanner that echoes what
it found puts the secret in the terminal, the scrollback, and any transcript of the session.

Exit codes: 0 clean, 1 credential found, 2 could not run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrub_settings import ALWAYS_SCRUB_PATTERNS, _secrets_from_env

REPO_ROOT = Path(__file__).resolve().parents[1]


def _staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        # The diff carries UTF-8; on Windows `text=True` alone decodes as cp1252 and dies on the
        # first em-dash in a docstring. `replace` keeps a mangled byte from masking a real match.
        encoding="utf-8",
        errors="replace",
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _staged_diff(path: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--cached", "--", path],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def main() -> int:
    try:
        files = _staged_files()
    except subprocess.CalledProcessError as exc:
        print(f"could not read the index: {exc}")
        return 2

    if not files:
        print("Nothing staged.")
        return 0

    secrets = _secrets_from_env()
    if not secrets:
        print("WARNING: no credential-shaped values found in .env — this check verified nothing.")
        return 0

    offenders: dict[str, int] = {}
    for path in files:
        diff = _staged_diff(path)
        hits = sum(diff.count(s) for s in secrets)
        hits += sum(len(p.findall(diff)) for p in ALWAYS_SCRUB_PATTERNS)
        if hits:
            offenders[path] = hits

    if offenders:
        print(f"CREDENTIAL FOUND in {len(offenders)} staged file(s) — commit refused:")
        for path, hits in sorted(offenders.items()):
            print(f"  {path}  ({hits} occurrence(s))")
        print()
        print("For .claude/settings.json, run: uv run python scripts/scrub_settings.py")
        return 1

    print(f"Clean — {len(files)} staged file(s), {len(secrets)} secret(s) checked for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
