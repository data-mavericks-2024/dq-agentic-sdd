"""Schema names, and the single place the ``${P}`` prefix is applied.

Tables are declared against **symbolic** schema names (``commercial``, ``dq``, …). The physical
name is the symbolic name with a prefix in front, and SQLAlchemy's ``schema_translate_map`` does
the substitution at execution time. That is what lets one predicate string run against
``commercial`` in development and ``test_20260824143022_a1b2c3_commercial`` under test with no
string rewriting — the property contracts/rule-definition.md depends on.

Raw DDL (triggers, grants) cannot go through the translate map, so it interpolates the physical
name. Every such interpolation goes through :func:`assert_safe_identifier` first.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

#: PostgreSQL unquoted-identifier shape, further narrowed to lower-case. Interpolating anything
#: else into DDL is rejected rather than quoted, because a schema name that needs quoting is a
#: configuration mistake, not a use case.
_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Schema(StrEnum):
    """The five schemas. Symbolic names, as used in table definitions."""

    COMMERCIAL = "commercial"
    DQ = "dq"
    WORKFLOW = "workflow"
    AUDIT = "audit"
    SANDBOX = "sandbox"


#: Creation order. `commercial` and `dq` hold Feature 1's tables; the other three are created
#: empty so that later features add tables rather than schemas, and so the grant defaults in
#: migration 0005 have somewhere to attach.
ALL_SCHEMAS: Final[tuple[Schema, ...]] = (
    Schema.COMMERCIAL,
    Schema.DQ,
    Schema.WORKFLOW,
    Schema.AUDIT,
    Schema.SANDBOX,
)


class UnsafeIdentifierError(ValueError):
    """An identifier failed validation before being interpolated into DDL."""


def assert_safe_identifier(name: str) -> str:
    """Return ``name`` if it is a bare lower-case SQL identifier, else raise.

    Called on every identifier that reaches a DDL string by interpolation rather than by bind
    parameter. DDL cannot be parameterised, so this check is the only thing between a malformed
    ``DQ_SCHEMA_PREFIX`` and injected DDL.
    """
    if not _IDENT_RE.match(name):
        raise UnsafeIdentifierError(
            f"{name!r} is not a bare lower-case SQL identifier "
            f"(pattern {_IDENT_RE.pattern!r}); refusing to interpolate it into DDL."
        )
    return name


def physical(schema: Schema, prefix: str = "") -> str:
    """Return the physical schema name for ``schema`` under ``prefix``."""
    return assert_safe_identifier(f"{prefix}{schema.value}")


def translate_map(prefix: str = "") -> dict[str, str]:
    """Return the SQLAlchemy ``schema_translate_map`` for ``prefix``.

    Applied to every connection, so table definitions never carry a physical name.
    """
    return {schema.value: physical(schema, prefix) for schema in ALL_SCHEMAS}


def search_path(prefix: str = "") -> str:
    """Return the ``search_path`` value the engine pins per run (research.md D10).

    Only ``commercial`` and ``dq`` are on it. Predicates use unqualified table names, so this is
    what resolves them — and pinning it is what makes the prefix mechanism deterministic instead of
    a hazard. ``pg_temp`` is last so a temporary object can never shadow a real table.
    """
    return f"{physical(Schema.COMMERCIAL, prefix)}, {physical(Schema.DQ, prefix)}, pg_temp"
