"""The Python validator and the database trigger agree (T035a).

Two implementations of one rule set. :mod:`dq.rules.predicates` runs at registration time for a
legible error; the ``BEFORE INSERT`` trigger runs in the database and is the actual enforcement,
because ``dq_author`` holds INSERT on ``rule_version`` and can reach it directly.

**Drift between them is silent, and it fails in the dangerous direction.** If the Python side grows
lax, a predicate sails through registration and is rejected by the trigger — noisy, but safe. If the
*trigger* grows lax while Python stays strict, nothing catches it: the registration path is not the
control, and a non-deterministic predicate reaches production having passed every test that only
exercised Python.

One corpus, asserted rejected by both. Where they disagree, the trigger is right and this suite
names the case that needs fixing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from dq.config.settings import Role, Settings
from dq.rules.predicates import RuleDefinitionError, validate_predicate

VALID = """
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


def _broken(find: str, replace: str) -> str:
    return VALID.replace(find, replace)


#: ``(label, predicate, rule)``. Every entry must be rejected by both implementations, and both must
#: blame the same rule — a case rejected by the right mechanism for the wrong reason is a case that
#: will stop being rejected when the wrong reason is fixed.
CORPUS: list[tuple[str, str, str]] = [
    ("now() is STABLE", _broken("h.npi IS NULL", "h.valid_from < now()::date"), "Rule 3"),
    ("CURRENT_DATE has no parens", _broken(":as_of_date", "CURRENT_DATE"), "Rule 3"),
    ("random() is VOLATILE", _broken("h.npi IS NULL", "random() < 0.5"), "Rule 3"),
    (
        "levenshtein is IMMUTABLE but forbidden",
        _broken("h.npi IS NULL", "levenshtein(h.last_name, 'Smith') < 3"),
        "FR-016d",
    ),
    (
        "similarity is IMMUTABLE but forbidden",
        _broken("h.npi IS NULL", "similarity(h.last_name, 'Smith') > 0.8"),
        "FR-016d",
    ),
    ("wrong projection", _broken("AS offending_value", "AS bad_value"), "Rule 2"),
    (
        "catalog table alongside a real one",
        _broken("FROM   hcp h", "FROM   hcp h, pg_catalog.pg_class pc"),
        "Rule 5",
    ),
    (
        "master join without the watermark",
        _broken("AND  h.batch_id   <= :reference_watermark", ""),
        "Rule 7",
    ),
    ("undeclared parameter", _broken("'^[0-9]{10}$'", ":some_pattern"), "Rule 8"),
    ("two statements", VALID + "; DROP TABLE hcp", "Rule 1"),
    (
        "function as a row source",
        _broken("FROM   hcp h", "FROM   generate_series(1, 10) g, hcp h"),
        "Rule 4",
    ),
    # Found by running the engine, not by reading it: this form passed all eight rules, registered
    # cleanly, and then could not execute. Both sides reject it now (migration 0007).
    (
        "postfix cast on a bound parameter",
        _broken(":as_of_date", ":as_of_date::date"),
        "Rule 8b",
    ),
]

Registrar = Callable[[str], None]


@pytest.fixture(scope="module")
def author_engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.role_url(Role.AUTHOR), future=True, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def register_raw(author_engine: Engine, schema_prefix: str) -> Registrar:
    """Insert a rule version as ``dq_author``, then roll back.

    Rolled back rather than committed because ``rule_version`` is immutable by trigger — a row a
    test committed could not afterwards be cleaned up.
    """
    dq = f"{schema_prefix}dq"

    def _register(predicate: str) -> None:
        with author_engine.connect() as conn:
            trans = conn.begin()
            try:
                rule_id = conn.execute(
                    text(
                        f'INSERT INTO "{dq}".rule (rule_key, domain, dimension, owning_function) '
                        "VALUES ('PARITY-PROBE', 'HCP', 'validity', 'Test') RETURNING rule_id"
                    )
                ).scalar_one()
                conn.execute(
                    text(
                        f'INSERT INTO "{dq}".rule_version '
                        "(rule_id, version_no, severity, subject_type, predicate_sql) "
                        "VALUES (:rid, 1, 'HIGH', 'record', :sql)"
                    ),
                    {"rid": rule_id, "sql": predicate},
                )
            finally:
                trans.rollback()

    return _register


@pytest.mark.parametrize(("label", "predicate", "rule"), CORPUS, ids=[c[0] for c in CORPUS])
def test_python_rejects(label: str, predicate: str, rule: str) -> None:
    with pytest.raises(RuleDefinitionError) as excinfo:
        validate_predicate(predicate, {})
    assert rule in str(excinfo.value), (
        f"{label}: Python rejected it, but blamed {excinfo.value} rather than {rule}."
    )


@pytest.mark.parametrize(("label", "predicate", "rule"), CORPUS, ids=[c[0] for c in CORPUS])
def test_trigger_rejects(register_raw: Registrar, label: str, predicate: str, rule: str) -> None:
    """The half that matters. This is the control; the Python check above is the courtesy."""
    with pytest.raises(DBAPIError) as excinfo:
        register_raw(predicate)
    message = str(excinfo.value.orig)
    assert rule in message, (
        f"{label}: the trigger rejected it, but blamed a different rule.\n"
        f"Expected {rule!r} in: {message}"
    )


def test_both_accept_the_valid_predicate(register_raw: Registrar) -> None:
    """Without this, every case above would pass against implementations that rejected everything."""
    validate_predicate(VALID, {})
    register_raw(VALID)
