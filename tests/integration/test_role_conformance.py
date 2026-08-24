"""The `dq_*` role set equals constitution v1.1.0 principle VI, exactly (T029).

Principle VI declares its list of seven exhaustive and requires this test by name: *"A conformance
test MUST assert that the set of ``dq_*`` roles present in the database equals this list, so that
adding one fails the build until this document is amended."*

The failure mode being guarded is gradual. No feature ever proposes dismantling least privilege;
each proposes one more role for one good reason, and the principle erodes by accumulation. An
eighth role fails here, which turns "should we add a role?" from a judgement call into an
amendment.

The expected set is read from the constitution file rather than restated, so the two cannot drift.
A test carrying its own copy of the list would keep passing after someone edited the constitution.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Engine, text

from dq.config.settings import Role

CONSTITUTION = Path(__file__).resolve().parents[2] / ".specify" / "memory" / "constitution.md"

#: Principle VI names each role in the first cell of a markdown table row, as `` `dq_x` ``.
_ROLE_CELL = re.compile(r"^\|\s*`(dq_[a-z]+)`\s*\|", re.MULTILINE)


def constitution_roles() -> set[str]:
    """The role names principle VI declares, parsed out of the constitution."""
    text_ = CONSTITUTION.read_text(encoding="utf-8")
    start = text_.index("### VI. Least-Privilege Data Access")
    end = text_.index("### VII.", start)
    return set(_ROLE_CELL.findall(text_[start:end]))


def test_constitution_names_seven_roles() -> None:
    """The parser works. Without this, a parse returning nothing makes every check below vacuous."""
    assert len(constitution_roles()) == 7


def test_settings_role_enum_matches_constitution() -> None:
    """`dq.config.settings.Role` is the code's copy of the list; it must match the document."""
    assert {r.value for r in Role} == constitution_roles()


def test_database_roles_equal_constitution(admin_engine: Engine, schema_prefix: str) -> None:
    """The roles actually present in the cluster equal the constitution's list.

    Depends on `schema_prefix` only for ordering: it guarantees migrations have run.
    """
    _ = schema_prefix
    with admin_engine.connect() as conn:
        present = {
            row[0]
            for row in conn.execute(
                text(r"SELECT rolname FROM pg_roles WHERE rolname LIKE 'dq\_%'")
            )
        }

    expected = constitution_roles()
    extra = present - expected
    missing = expected - present

    assert not extra, (
        f"Roles present in the database but not in constitution principle VI: {sorted(extra)}. "
        f"Introducing a role is an amendment to the constitution, not a planning decision."
    )
    assert not missing, (
        f"Roles named in constitution principle VI but absent from the database: "
        f"{sorted(missing)}. Run `alembic upgrade head` with DQ_SCHEMA_PREFIX unset."
    )


def test_no_role_inherits_another(admin_engine: Engine, schema_prefix: str) -> None:
    """Every `dq_*` role is NOINHERIT, and none is a member of another.

    Principle VI says no role may inherit another's privileges. NOINHERIT alone is not enough:
    a role that is a *member* of another can still `SET ROLE` to it. Both are checked, because
    either one on its own leaves the separation nominal.
    """
    _ = schema_prefix
    with admin_engine.connect() as conn:
        inheriting = [
            row[0]
            for row in conn.execute(
                text(r"SELECT rolname FROM pg_roles WHERE rolname LIKE 'dq\_%' AND rolinherit")
            )
        ]
        cross_membership = [
            (row[0], row[1])
            for row in conn.execute(
                text(
                    r"""
                    SELECT m.rolname AS member, g.rolname AS granted
                    FROM pg_auth_members am
                    JOIN pg_roles m ON m.oid = am.member
                    JOIN pg_roles g ON g.oid = am.roleid
                    WHERE m.rolname LIKE 'dq\_%' AND g.rolname LIKE 'dq\_%'
                    """
                )
            )
        ]

    assert not inheriting, f"These roles are INHERIT and must be NOINHERIT: {sorted(inheriting)}"
    assert not cross_membership, (
        f"These dq_* roles are members of other dq_* roles: {cross_membership}. "
        f"No role may reach another's privileges."
    )
