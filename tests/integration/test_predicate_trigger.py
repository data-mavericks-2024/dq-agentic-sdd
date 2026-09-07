"""The `rule_version` predicate-validation trigger actually fires and actually rejects (T025).

Constitution principle I requires determinism to be enforced structurally, not by convention. The
trigger is the structure. These tests exercise it **as `dq_author`** — the role that holds INSERT
on `rule_version` and could therefore bypass any check living only in the Python registration path.
Running them as a superuser would prove nothing about the case that matters.

The exhaustive per-rule unit tests are T031/T031a, and the shared-corpus parity check between the
Python validator and this trigger is T035a; both are Phase 3. What is here is the evidence that
T025 works at all: predicates that must be accepted, and one rejection per validation rule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from dq.config.settings import Role, Settings, load_settings

pytestmark = pytest.mark.usefixtures("schema_prefix")

#: Registers one rule version and rolls back; raises whatever the trigger raised.
Registrar = Callable[..., None]

#: A predicate satisfying all eight rules. Every rejection case below is this, minimally broken,
#: so a failure points at one rule rather than at the example.
VALID_PREDICATE = """
SELECT h.hcp_id::text AS subject_key,
       h.npi          AS offending_value,
       NULL::text     AS observed_value,
       NULL::text     AS expected_value
FROM   hcp h
WHERE  h.batch_id = :batch_id
  AND  h.valid_from <= :as_of_date
  AND  h.batch_id   <= :reference_watermark
  AND  (h.npi IS NULL OR h.npi !~ '^[0-9]{10}$')
"""


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="module")
def author(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.role_url(Role.AUTHOR), future=True, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def register(author: Engine, schema_prefix: str) -> Registrar:
    """Register a rule version as `dq_author`, then roll back.

    Rolled back rather than committed because `rule_version` is immutable by trigger — a row a test
    committed could not afterwards be cleaned up.

    The two INSERTs are schema-qualified but the **predicate is not**, and this connection's
    ``search_path`` never includes the commercial schema. So every acceptance below is also proof
    that the trigger's own baked-in ``SET search_path`` is what resolves ``hcp`` — the mechanism
    that lets one predicate string run against `commercial` in development and against a prefixed
    test schema here, with no string rewriting (contracts/rule-definition.md).
    """
    dq = f"{schema_prefix}dq"

    def _register(predicate: str, *, parameters: str = "{}") -> None:
        with author.connect() as conn:
            trans = conn.begin()
            try:
                rule_id = conn.execute(
                    text(
                        f'INSERT INTO "{dq}".rule (rule_key, domain, dimension, owning_function) '
                        "VALUES ('TEST-PREDICATE', 'HCP', 'validity', 'Test') RETURNING rule_id"
                    )
                ).scalar_one()
                conn.execute(
                    text(
                        f'INSERT INTO "{dq}".rule_version '
                        "(rule_id, version_no, severity, subject_type, predicate_sql, parameters) "
                        "VALUES (:rid, 1, 'HIGH', 'record', :sql, CAST(:params AS jsonb))"
                    ),
                    {"rid": rule_id, "sql": predicate, "params": parameters},
                )
            finally:
                trans.rollback()

    return _register


def _assert_rejected(register: Registrar, predicate: str, expected: str) -> None:
    """The trigger must refuse ``predicate``, naming the rule it broke."""
    # `DBAPIError` rather than a narrower class: the trigger raises `check_violation`, which
    # SQLAlchemy maps to IntegrityError, while a predicate that fails to plan surfaces as
    # ProgrammingError. Both are legitimate rejections and both must be caught here.
    with pytest.raises(DBAPIError) as excinfo:
        register(predicate)
    message = str(excinfo.value.orig)
    assert expected in message, (
        f"Rejected, but not for the expected reason.\n"
        f"Expected to see: {expected!r}\nActual message: {message}"
    )


# ---------------------------------------------------------------------------
# Acceptance. Without these, every rejection below would pass against a trigger
# that raised unconditionally — a different bug wearing the same green tick.
# ---------------------------------------------------------------------------


def test_a_valid_predicate_is_accepted(register: Registrar) -> None:
    register(VALID_PREDICATE)


def test_accepts_a_declared_parameter(register: Registrar) -> None:
    """Rule 8's complement: a `:name` present in `parameters` is fine."""
    register(
        VALID_PREDICATE.replace("'^[0-9]{10}$'", ":npi_pattern"),
        parameters='{"npi_pattern": "^[0-9]{10}$"}',
    )


# ---------------------------------------------------------------------------
# Rule 3 — every function in an expression must be IMMUTABLE.
# ---------------------------------------------------------------------------


def test_rejects_now(register: Registrar) -> None:
    """`now()` is STABLE, not VOLATILE. A "reject volatile" rule would have admitted it.

    Added alongside `:as_of_date` rather than in place of it, so rule 7 does not fire first and
    mask what is being tested.
    """
    _assert_rejected(
        register,
        VALID_PREDICATE.replace("h.npi IS NULL", "h.valid_from < now()::date"),
        "Rule 3",
    )


def test_rejects_current_date(register: Registrar) -> None:
    """`CURRENT_DATE` is the first function a timeliness-rule author reaches for.

    Revision 1's denylist of six function names omitted it. This specific miss is why the design
    became an allowlist over IMMUTABLE — complete by construction rather than incomplete by nature.
    """
    _assert_rejected(register, VALID_PREDICATE.replace(":as_of_date", "CURRENT_DATE"), "Rule 3")


def test_rejects_random(register: Registrar) -> None:
    _assert_rejected(register, VALID_PREDICATE.replace("h.npi IS NULL", "random() < 0.5"), "Rule 3")


# ---------------------------------------------------------------------------
# The remaining rules.
# ---------------------------------------------------------------------------


def test_rejects_levenshtein(register: Registrar) -> None:
    """FR-016d. `levenshtein(a, b) < 3` is IMMUTABLE and passes every other check here."""
    _assert_rejected(
        register,
        VALID_PREDICATE.replace("h.npi IS NULL", "levenshtein(h.last_name, 'Smith') < 3"),
        "FR-016d",
    )


def test_rejects_wrong_projection(register: Registrar) -> None:
    """Rule 2 — the four contract columns, exactly."""
    _assert_rejected(
        register, VALID_PREDICATE.replace("AS offending_value", "AS bad_value"), "Rule 2"
    )


def test_rejects_catalog_table(register: Registrar) -> None:
    """Rule 5 — only allowlisted tables.

    The catalog is joined *alongside* `hcp` rather than replacing it, so the predicate still plans
    and the check under test is the one that rejects it. Replacing `hcp` outright fails earlier, on
    a column that does not exist — a rejection, but not this rejection.

    This case is also why rule 5 reads the parse tree: `pg_class` is a pinned system object, so it
    produces no `pg_depend` row and a dependency-based check would let it through.
    """
    _assert_rejected(
        register,
        VALID_PREDICATE.replace("FROM   hcp h", "FROM   hcp h, pg_catalog.pg_class pc"),
        "Rule 5",
    )


def test_rejects_master_join_without_watermark(register: Registrar) -> None:
    """Rule 7 — the most dangerous omission available in this contract.

    A predicate missing `:reference_watermark` returns plausible results and passes the determinism
    test, which runs twice within the same minute — while silently breaking every historical
    re-run. Nothing but a structural check catches it.
    """
    _assert_rejected(
        register,
        VALID_PREDICATE.replace("AND  h.batch_id   <= :reference_watermark", ""),
        "Rule 7",
    )


def test_rejects_alignment_reference_without_watermark(register: Registrar) -> None:
    """Rule 7 also protects alignment rows delivered after a historical run."""
    predicate = """
SELECT a.alignment_id::text AS subject_key,
       a.territory_code     AS offending_value,
       NULL::text           AS observed_value,
       NULL::text           AS expected_value
FROM   territory_alignment a
WHERE  a.batch_id = :batch_id
  AND  EXISTS (
         SELECT 1
         FROM   territory_alignment later
         WHERE  later.source_system_id = a.source_system_id
           AND  later.hcp_source_key = a.hcp_source_key
           AND  later.effective && a.effective
       )
"""
    _assert_rejected(register, predicate, "Rule 7")


def test_rejects_data_batch_reference_without_watermark(register: Registrar) -> None:
    """Rule 7 protects feed predicates from batches delivered after the pinned world."""
    predicate = """
SELECT b.batch_id::text AS subject_key,
       b.arrival_ts::text AS offending_value,
       NULL::text AS observed_value,
       NULL::text AS expected_value
FROM   data_batch b
WHERE  b.source_system_id = :scope_source_system_id
  AND  b.business_period && daterange(
         CAST(:scope_period_start AS date), CAST(:scope_period_end AS date), '[)'
       )
"""
    _assert_rejected(register, predicate, "Rule 7")


def test_rejects_undeclared_parameter(register: Registrar) -> None:
    """Rule 8."""
    _assert_rejected(register, VALID_PREDICATE.replace("'^[0-9]{10}$'", ":some_pattern"), "Rule 8")


def test_rejects_multiple_statements(register: Registrar) -> None:
    """Rule 1."""
    _assert_rejected(register, VALID_PREDICATE + "; DROP TABLE hcp", "Rule 1")


def test_rejects_function_in_from(register: Registrar) -> None:
    """Rule 4 keeps rule 3 total: `generate_series` is IMMUTABLE, so volatility alone admits it."""
    _assert_rejected(
        register,
        VALID_PREDICATE.replace("FROM   hcp h", "FROM   generate_series(1, 10) h(hcp_id), hcp h2"),
        "Rule 4",
    )
