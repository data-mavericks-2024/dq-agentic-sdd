"""Environment loading and per-role connection resolution.

Every database connection in this project is resolved here, by role. Nothing else reads
``os.environ`` for a connection string, so "which role am I connected as?" has exactly one
answer per code path (constitution principle VI).

Two rules this module exists to enforce:

* **Fail fast on a missing variable.** A missing ``DQ_ENGINE_URL`` must be a startup error, not a
  fallback to whatever else is configured. Silently falling back to a more privileged connection is
  the failure mode principle VI exists to prevent.
* **Never log a URL.** Connection strings carry passwords. :func:`redact` is the only sanctioned way
  to put one in a message.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[3]

# Schema prefixes are interpolated into DDL. Anything outside this pattern is rejected before it
# reaches the database — see dq.db.schemas.assert_safe_identifier.
_PREFIX_RE = re.compile(r"^[a-z0-9_]*$")


class Role(StrEnum):
    """The seven roles named in constitution v1.1.0 principle VI. This list is exhaustive."""

    MIGRATE = "dq_migrate"
    INGEST = "dq_ingest"
    AUTHOR = "dq_author"
    ENGINE = "dq_engine"
    READONLY = "dq_readonly"
    SANDBOX = "dq_sandbox"
    PUBLISH = "dq_publish"


#: Environment variable holding each role's connection URL.
ROLE_URL_VAR: dict[Role, str] = {
    Role.MIGRATE: "DQ_MIGRATE_URL",
    Role.INGEST: "DQ_INGEST_URL",
    Role.AUTHOR: "DQ_AUTHOR_URL",
    Role.ENGINE: "DQ_ENGINE_URL",
    Role.READONLY: "DQ_READONLY_URL",
    Role.SANDBOX: "DQ_SANDBOX_URL",
    Role.PUBLISH: "DQ_PUBLISH_URL",
}

#: Environment variable holding each role's password, read only by the bootstrap migration.
ROLE_PASSWORD_VAR: dict[Role, str] = {role: f"{role.name}_DQ_PASSWORD" for role in Role}


class ConfigError(RuntimeError):
    """A required environment variable is missing or malformed."""


def load_dotenv(path: Path | None = None, *, override: bool = False) -> None:
    """Populate ``os.environ`` from a ``.env`` file.

    Deliberately minimal — no dependency, no interpolation, no export syntax. Values are taken
    verbatim after the first ``=``, with surrounding quotes stripped.
    """
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def require(name: str) -> str:
    """Return ``os.environ[name]`` or raise :class:`ConfigError` naming the variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Required environment variable {name} is unset or empty. "
            f"See .env.example for its shape. Never substitute a different connection."
        )
    return value


def redact(url: str) -> str:
    """Return ``url`` with the password replaced, safe to log or raise."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.hostname:
        return "<unparseable-url>"
    user = parts.username or ""
    netloc = f"{user}:***@{parts.hostname}" if user else str(parts.hostname)
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def as_psycopg_url(url: str) -> str:
    """Normalise a Supabase connection string to the ``postgresql+psycopg`` SQLAlchemy dialect.

    Also percent-encodes the password when ``SUPABASE_DB_PASSWORD`` is used to fill a
    ``[YOUR-PASSWORD]`` placeholder, which is how the Supabase dashboard hands the URL out.
    """
    url = url.strip().strip('"').strip("'")
    placeholder = "[YOUR-PASSWORD]"
    if placeholder in url:
        password = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
        if not password:
            raise ConfigError(
                f"Connection string still contains {placeholder} and SUPABASE_DB_PASSWORD is unset."
            )
        url = url.replace(placeholder, quote(password, safe=""))
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise ConfigError(f"Connection string is not a PostgreSQL URL: {redact(url)}")


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration for one process.

    ``stateful_url`` is excluded from ``repr`` and the generated one is replaced, because a
    connection string carries a password and dataclass reprs surface in places nobody audits —
    pytest fixture headers, exception context, log lines, a debugger. This is not defence in depth;
    it is the only defence, since every one of those sites is outside this module's control.
    """

    schema_prefix: str
    stateful_url: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"Settings(schema_prefix={self.schema_prefix!r}, "
            f"stateful_url={redact(self.stateful_url)!r})"
        )

    @property
    def is_prefixed(self) -> bool:
        """True when running against isolated test schemas.

        The bootstrap migration keys off this: roles are cluster-global, so a prefixed run must
        never create or drop them (research.md D5).
        """
        return bool(self.schema_prefix)

    def role_url(self, role: Role) -> str:
        """Connection URL for ``role``. Raises if the variable is unset — never falls back."""
        return as_psycopg_url(require(ROLE_URL_VAR[role]))

    def role_url_or_none(self, role: Role) -> str | None:
        """Connection URL for ``role``, or ``None`` when the variable is unset.

        Used only by the migration entry point, which must run as the superuser on the very first
        upgrade because ``dq_migrate`` does not exist yet.
        """
        raw = os.environ.get(ROLE_URL_VAR[role], "").strip()
        return as_psycopg_url(raw) if raw else None


def load_settings(*, dotenv: bool = True) -> Settings:
    """Read the environment and return validated :class:`Settings`."""
    if dotenv:
        load_dotenv()

    prefix = os.environ.get("DQ_SCHEMA_PREFIX", "").strip()
    if not _PREFIX_RE.match(prefix):
        raise ConfigError(
            f"DQ_SCHEMA_PREFIX must match {_PREFIX_RE.pattern!r}; got {prefix!r}. "
            f"It is interpolated into DDL, so anything else is rejected here."
        )

    return Settings(
        schema_prefix=prefix,
        stateful_url=as_psycopg_url(require("SUPABASE_DB_STATEFUL_URL")),
    )
