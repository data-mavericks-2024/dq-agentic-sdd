"""Least privilege, verified by attempting the writes and being refused (T030).

Constitution principle II says no agent may mutate curated commercial data; principle VI is the
mechanism. This is the test that makes the mechanism true rather than asserted.

**Every check attempts the operation.** A privilege test that passes because the code never tried
proves nothing at all — it is indistinguishable from a test against a role with full rights. So
each case issues real DDL or DML and asserts PostgreSQL refuses it.

The writes are attempted inside a transaction that is rolled back regardless. If a grant were ever
loosened by mistake, this test would fail on the *absence* of the exception before it could commit
anything.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from dq.config.settings import Role, Settings, load_settings
from dq.db.engine import RoleAssertionError, assert_current_user
from dq.db.schemas import Schema, physical

pytestmark = pytest.mark.usefixtures("schema_prefix")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings()


def _engine_for(settings: Settings, role: Role) -> Engine:
    return create_engine(settings.role_url(role), future=True, pool_pre_ping=True)


@pytest.fixture
def engine_role(settings: Settings) -> Iterator[dict[Role, Engine]]:
    """One engine per role under test, disposed together."""
    engines = {role: _engine_for(settings, role) for role in (Role.ENGINE, Role.AUTHOR)}
    yield engines
    for engine in engines.values():
        engine.dispose()


def _assert_refused(engine: Engine, statement: str, what: str) -> None:
    """Execute ``statement`` and require PostgreSQL to refuse it on privilege grounds."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(ProgrammingError) as excinfo:
                conn.execute(text(statement))
            # `InsufficientPrivilege` is SQLSTATE 42501. Asserting the code rather than the
            # message keeps this from passing on an unrelated failure — a typo in the statement
            # would otherwise look like a successful refusal.
            assert getattr(excinfo.value.orig, "sqlstate", None) == "42501", (
                f"{what} failed, but not for lack of privilege: {excinfo.value.orig}"
            )
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# dq_engine may read commercial data and may not write it.
# ---------------------------------------------------------------------------


def test_engine_can_read_commercial(engine_role: dict[Role, Engine], schema_prefix: str) -> None:
    """The negative tests below are only meaningful if the role can reach the table at all."""
    commercial = physical(Schema.COMMERCIAL, schema_prefix)
    with engine_role[Role.ENGINE].connect() as conn:
        count = conn.execute(text(f'SELECT count(*) FROM "{commercial}".hcp')).scalar_one()
    assert count == 0


@pytest.mark.parametrize(
    ("what", "template"),
    [
        (
            "INSERT INTO commercial.sales_transaction",
            'INSERT INTO "{c}".sales_transaction '
            "(batch_id, source_system_id, source_txn_key, product_key, hcp_key, "
            " territory_code, txn_date, quantity, uom) "
            "VALUES (1, 1, 'x', 'p', 'h', 't', DATE '2026-01-01', 1, 'EA')",
        ),
        ("UPDATE commercial.hcp", "UPDATE \"{c}\".hcp SET npi = '9999999999'"),
        ("DELETE FROM commercial.product", 'DELETE FROM "{c}".product'),
        ("CREATE TABLE commercial.scratch", 'CREATE TABLE "{c}".scratch (id int)'),
    ],
)
def test_engine_cannot_write_commercial(
    engine_role: dict[Role, Engine], schema_prefix: str, what: str, template: str
) -> None:
    """Each write is attempted against real commercial tables and must be refused."""
    commercial = physical(Schema.COMMERCIAL, schema_prefix)
    _assert_refused(engine_role[Role.ENGINE], template.format(c=commercial), what)


@pytest.fixture
def seeded_source_system(admin_engine: Engine, schema_prefix: str) -> Iterator[int]:
    """A `source_system` row, inserted by an authorised role and removed afterwards.

    `rule_run` carries a foreign key to it, so a scoped run cannot be written without one.
    Inserted via the admin connection because seeding commercial data is `dq_ingest`'s job, not
    `dq_engine`'s — using dq_engine here would be testing the wrong grant.
    """
    commercial = physical(Schema.COMMERCIAL, schema_prefix)
    with admin_engine.begin() as conn:
        conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
        source_id = conn.execute(
            text(
                f'INSERT INTO "{commercial}".source_system (code, name) '
                "VALUES ('TESTSRC', 'Privilege test source') RETURNING source_system_id"
            )
        ).scalar_one()
        conn.execute(text("RESET ROLE"))

    yield int(source_id)

    with admin_engine.begin() as conn:
        conn.execute(text(f"SET ROLE {Role.MIGRATE.value}"))
        conn.execute(
            text(f'DELETE FROM "{commercial}".source_system WHERE source_system_id = :i'),
            {"i": source_id},
        )
        conn.execute(text("RESET ROLE"))


def test_engine_can_write_findings(
    engine_role: dict[Role, Engine], schema_prefix: str, seeded_source_system: int
) -> None:
    """The grant is narrow, not absent — dq_engine exists in order to write rule runs and findings.

    A suite that only proved what a role *cannot* do would pass just as well against a role with no
    privileges at all, which would be a different bug wearing the same green tick.
    """
    dq = physical(Schema.DQ, schema_prefix)
    with engine_role[Role.ENGINE].connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(
                text(
                    f'INSERT INTO "{dq}".rule_run '
                    "(correlation_id, scope_type, scope_source_system_id, scope_period, "
                    " as_of_date, reference_watermark, session_settings, status) "
                    "VALUES (gen_random_uuid(), 'source_period', :src, "
                    " daterange(DATE '2026-01-01', DATE '2026-02-01', '[)'), "
                    " DATE '2026-01-31', 0, '{}'::jsonb, 'RUNNING')"
                ),
                {"src": seeded_source_system},
            )
        finally:
            trans.rollback()


def test_engine_cannot_write_an_incoherent_scope(
    engine_role: dict[Role, Engine], schema_prefix: str
) -> None:
    """A `source_period` run with no source and no period is rejected by the schema.

    Not a privilege check — a check that the scope columns cannot disagree with `scope_type`. A run
    recording a scope it did not evaluate would make every finding under it unattributable.
    """
    dq = physical(Schema.DQ, schema_prefix)
    with engine_role[Role.ENGINE].connect() as conn:
        trans = conn.begin()
        try:
            with pytest.raises(IntegrityError) as excinfo:
                conn.execute(
                    text(
                        f'INSERT INTO "{dq}".rule_run '
                        "(correlation_id, scope_type, as_of_date, reference_watermark, "
                        " session_settings, status) "
                        "VALUES (gen_random_uuid(), 'source_period', DATE '2026-01-01', 0, "
                        " '{}'::jsonb, 'RUNNING')"
                    )
                )
            assert "period_scope_has_source_and_period" in str(excinfo.value.orig)
        finally:
            trans.rollback()


# ---------------------------------------------------------------------------
# dq_author governs the rule registry and has no commercial access at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["hcp", "hco", "product", "territory", "sales_transaction"])
def test_author_cannot_read_commercial(
    engine_role: dict[Role, Engine], schema_prefix: str, table: str
) -> None:
    """Rule registration must not be a path to reading commercial data.

    This is also what makes the T025 validation function's SECURITY DEFINER necessary: dq_author
    genuinely cannot see these tables, so it could not plan a predicate over them otherwise.
    """
    commercial = physical(Schema.COMMERCIAL, schema_prefix)
    _assert_refused(
        engine_role[Role.AUTHOR],
        f'SELECT * FROM "{commercial}".{table} LIMIT 1',
        f"dq_author SELECT on commercial.{table}",
    )


def test_author_can_read_rule_registry(engine_role: dict[Role, Engine], schema_prefix: str) -> None:
    """The complement: dq_author's own tables are reachable."""
    dq = physical(Schema.DQ, schema_prefix)
    with engine_role[Role.AUTHOR].connect() as conn:
        assert conn.execute(text(f'SELECT count(*) FROM "{dq}".rule')).scalar_one() == 0


# ---------------------------------------------------------------------------
# The identity assertion itself.
# ---------------------------------------------------------------------------


def test_current_user_assertion_rejects_a_mismatched_url(settings: Settings) -> None:
    """`assert_current_user` catches a URL pointing at the wrong role.

    Without it, pointing DQ_ENGINE_URL at dq_migrate voids principle II silently: the writes above
    would simply succeed and nothing would report it.
    """
    engine = create_engine(settings.role_url(Role.AUTHOR), future=True)
    try:
        with engine.connect() as conn, pytest.raises(RoleAssertionError):
            assert_current_user(conn, Role.ENGINE)
    finally:
        engine.dispose()


def test_current_user_assertion_accepts_the_right_role(settings: Settings) -> None:
    engine = create_engine(settings.role_url(Role.ENGINE), future=True)
    try:
        with engine.connect() as conn:
            assert_current_user(conn, Role.ENGINE)
    finally:
        engine.dispose()
