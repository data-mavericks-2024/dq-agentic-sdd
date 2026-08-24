"""Per-role connection factory.

Two guarantees live here, and nowhere else:

**The connected role is what it claims to be.** :func:`connect` asserts ``current_user`` matches
the requested role at connection open. Without it, pointing ``DQ_ENGINE_URL`` at ``dq_migrate``
voids constitution principle II silently — the writes the privilege model is supposed to refuse
would simply succeed, and no test would notice (data-model.md § Required privilege test).

**The execution environment is pinned.** Four ``SET LOCAL`` settings are issued before any
predicate runs, and recorded on ``rule_run.session_settings`` (research.md D10). Leaving any of
them to the client's default makes a rule's verdict depend on who ran it:

* ``TimeZone`` — every ``timestamptz → date`` cast resolves against it, and FR-021b's late/missing
  logic necessarily crosses that boundary. A feed late in UTC is on time in ``Asia/Singapore``.
* ``DateStyle`` — text-to-date coercion in a predicate.
* ``search_path`` — predicates use unqualified names so the schema prefix can resolve.
* ``statement_timeout`` — a rule near the limit yields ``ERRORED`` on one run and ``EVALUATED`` on
  the next, producing different finding sets from identical inputs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from dq.config.settings import Role, Settings
from dq.db import schemas

#: Pinned session settings, recorded verbatim on `rule_run.session_settings`.
SESSION_SETTINGS: Final[dict[str, str]] = {
    "TimeZone": "UTC",
    "DateStyle": "ISO, YMD",
    "statement_timeout": "15min",
}


class RoleAssertionError(RuntimeError):
    """The connected database user is not the role the caller asked for."""


def _engine_kwargs(url: str) -> dict[str, Any]:
    """Connection arguments appropriate to the endpoint.

    The Supabase **session** pooler holds one server connection per client, so prepared statements
    and advisory locks work — both verified on this endpoint. The transaction pooler (port 6543)
    breaks both silently and must never appear here; ``check_port`` makes that a startup error
    rather than intermittent flakiness.
    """
    parsed = make_url(url)
    if parsed.port == 6543:
        raise ValueError(
            "Port 6543 is the Supabase transaction pooler. It breaks prepared statements and "
            "advisory locks silently. Use the session pooler (5432) for anything stateful."
        )
    return {
        "pool_pre_ping": True,
        "future": True,
        # Supabase free tier is t4g.nano. A small pool avoids exhausting its connection budget.
        "pool_size": 3,
        "max_overflow": 2,
    }


def make_engine(url: str, prefix: str = "") -> Engine:
    """Build an :class:`Engine` whose connections resolve symbolic schema names under ``prefix``."""
    engine = create_engine(url, **_engine_kwargs(url))
    return engine.execution_options(schema_translate_map=schemas.translate_map(prefix))


def assert_current_user(conn: Connection, expected: Role) -> None:
    """Raise :class:`RoleAssertionError` unless the session user is ``expected``."""
    actual = conn.execute(text("SELECT current_user")).scalar_one()
    if actual != expected.value:
        raise RoleAssertionError(
            f"Connected as {actual!r} but this code path requires {expected.value!r}. "
            f"Check the {expected.name}_URL environment variable. "
            f"Continuing would void the privilege guarantee this role exists to provide."
        )


def pin_session(conn: Connection, prefix: str = "") -> dict[str, str]:
    """Issue the four ``SET LOCAL`` settings and return what was pinned.

    ``SET LOCAL`` is transaction-scoped, so this must run inside the transaction that will execute
    the predicates. The returned dict is what belongs in ``rule_run.session_settings``.
    """
    pinned = dict(SESSION_SETTINGS)
    pinned["search_path"] = schemas.search_path(prefix)
    for key, value in pinned.items():
        # SET LOCAL takes no bind parameters. `key` is from the module-level constant above and
        # `search_path` is built from validated identifiers, so neither is caller-controlled.
        conn.execute(text(f"SET LOCAL {key} = :value").bindparams(value=value))
    return pinned


@contextmanager
def connect(
    settings: Settings,
    role: Role,
    *,
    pin: bool = True,
) -> Iterator[Connection]:
    """Open a transaction as ``role``, asserting identity and pinning the session.

    The transaction commits on clean exit and rolls back on exception.
    """
    engine = make_engine(settings.role_url(role), settings.schema_prefix)
    try:
        with engine.begin() as conn:
            assert_current_user(conn, role)
            if pin:
                pin_session(conn, settings.schema_prefix)
            yield conn
    finally:
        engine.dispose()
