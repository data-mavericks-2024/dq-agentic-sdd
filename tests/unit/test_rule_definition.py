"""Rule definition and predicate validation (T031, T031a).

One failing case per validation rule in contracts/rule-definition.md, plus FR-016d. No database:
these are the fast half of the pair, and `tests/integration/test_validation_parity.py` (T035a) is
what proves this module and the trigger agree.

Every rejection case is :data:`VALID_PREDICATE` minimally broken, so a failure points at the rule
under test rather than at the example.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from dq.domain.dq import DIMENSIONS, DOMAINS, SEVERITIES, SUBJECT_TYPES
from dq.rules.definition import Dimension, Domain, RuleDefinition, Severity, SubjectType
from dq.rules.predicates import RuleDefinitionError, validate_predicate

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

BASE_FIELDS = {
    "rule_key": "TEST-RULE",
    "domain": "HCP",
    "dimension": "validity",
    "severity": "HIGH",
    "owning_function": "Master Data Management",
    "subject_type": "record",
}


def make(predicate: str = VALID_PREDICATE, **overrides: object) -> RuleDefinition:
    return RuleDefinition.model_validate({**BASE_FIELDS, "predicate_sql": predicate, **overrides})


def assert_rejected(
    predicate: str, expected: str, parameters: dict[str, object] | None = None
) -> None:
    with pytest.raises(RuleDefinitionError) as excinfo:
        validate_predicate(predicate, parameters or {})
    assert expected in str(excinfo.value), (
        f"Rejected, but not for the expected reason.\n"
        f"Expected to see: {expected!r}\nActual: {excinfo.value}"
    )


# ---------------------------------------------------------------------------
# Acceptance. Without these, every rejection below would pass against a
# validator that raised unconditionally — a different bug wearing the same tick.
# ---------------------------------------------------------------------------


def test_a_valid_definition_is_accepted() -> None:
    rule = make()
    assert rule.rule_key == "TEST-RULE"
    assert rule.parameters == {}


def test_accepts_a_declared_parameter() -> None:
    rule = make(
        VALID_PREDICATE.replace("'^[0-9]{10}$'", ":npi_pattern"),
        parameters={"npi_pattern": "^[0-9]{10}$"},
    )
    assert rule.parameters["npi_pattern"] == "^[0-9]{10}$"


def test_accepts_the_as_of_join_idiom() -> None:
    """The idiom every predicate touching master data must use. If this is rejected, the whole
    library is unregisterable."""
    validate_predicate(
        """
        SELECT t.txn_id::text AS subject_key,
               t.uom          AS offending_value,
               t.uom          AS observed_value,
               p.uom          AS expected_value
        FROM   sales_transaction t
        JOIN   LATERAL (
                 SELECT p2.uom, p2.is_deleted
                 FROM   product p2
                 WHERE  p2.source_system_id = t.source_system_id
                   AND  p2.source_key       = t.product_key
                   AND  p2.valid_from      <= :as_of_date
                   AND  p2.batch_id        <= :reference_watermark
                 ORDER BY p2.valid_from DESC
                 LIMIT  1
               ) p ON NOT p.is_deleted
        WHERE  t.batch_id = :batch_id AND t.uom <> p.uom
        """
    )


def test_cast_is_not_mistaken_for_a_parameter() -> None:
    """`::` and `:name` share a character.

    Without stripping casts first, `h.hcp_id::text` scans as a parameter named `text` and rule 8
    rejects every well-formed predicate in the library. This is a real bug that was fixed, not a
    hypothetical.
    """
    validate_predicate(VALID_PREDICATE)


# ---------------------------------------------------------------------------
# Rule 1 — a single SELECT.
# ---------------------------------------------------------------------------


def test_rejects_multiple_statements() -> None:
    assert_rejected(VALID_PREDICATE + "; DROP TABLE hcp", "Rule 1")


def test_rejects_dml() -> None:
    assert_rejected("DELETE FROM hcp WHERE npi IS NULL", "Rule 1")


def test_rejects_a_keyword_only_inside_a_literal_is_allowed() -> None:
    """The complement of rule 1: a literal containing SQL text is data, not a statement."""
    validate_predicate(VALID_PREDICATE.replace("'^[0-9]{10}$'", "'DROP TABLE hcp'"))


# ---------------------------------------------------------------------------
# Rule 2 — the four contract columns.
# ---------------------------------------------------------------------------


def test_rejects_wrong_projection() -> None:
    assert_rejected(VALID_PREDICATE.replace("AS offending_value", "AS bad_value"), "Rule 2")


def test_rejects_missing_column() -> None:
    assert_rejected(VALID_PREDICATE.replace("NULL::text     AS expected_value", "NULL"), "Rule 2")


# ---------------------------------------------------------------------------
# Rule 3 — IMMUTABLE only.
# ---------------------------------------------------------------------------


def test_rejects_now() -> None:
    """`now()` is STABLE, not VOLATILE. A "reject volatile" rule would have admitted it."""
    assert_rejected(
        VALID_PREDICATE.replace("h.npi IS NULL", "h.valid_from < now()::date"), "Rule 3"
    )


def test_rejects_current_date() -> None:
    """The first function a timeliness-rule author reaches for.

    Revision 1's denylist of six names omitted it, and it takes no parentheses, so no `name(`
    pattern finds it. This specific miss is why the trigger became an allowlist over IMMUTABLE.
    """
    assert_rejected(VALID_PREDICATE.replace(":as_of_date", "CURRENT_DATE"), "Rule 3")


def test_rejects_random() -> None:
    assert_rejected(VALID_PREDICATE.replace("h.npi IS NULL", "random() < 0.5"), "Rule 3")


# ---------------------------------------------------------------------------
# Rule 4 — no function in FROM.
# ---------------------------------------------------------------------------


def test_rejects_function_in_from() -> None:
    """Keeps rule 3 total: `generate_series` is IMMUTABLE, so volatility alone admits it."""
    assert_rejected(
        VALID_PREDICATE.replace("FROM   hcp h", "FROM   generate_series(1, 10) g, hcp h"), "Rule 4"
    )


# ---------------------------------------------------------------------------
# Rule 5 — unqualified, allowlisted tables.
# ---------------------------------------------------------------------------


def test_rejects_schema_qualified_table() -> None:
    assert_rejected(VALID_PREDICATE.replace("FROM   hcp h", "FROM   commercial.hcp h"), "Rule 5")


def test_rejects_catalog_table() -> None:
    assert_rejected(
        VALID_PREDICATE.replace("FROM   hcp h", "FROM   hcp h, pg_catalog.pg_class pc"), "Rule 5"
    )


def test_rejects_non_allowlisted_table() -> None:
    """A rule reading `finding` would be circular."""
    assert_rejected(VALID_PREDICATE.replace("FROM   hcp h", "FROM   finding h"), "Rule 5")


# ---------------------------------------------------------------------------
# Rule 6 — total ordering wherever order decides the result.
# ---------------------------------------------------------------------------


def test_rejects_limit_without_order_by() -> None:
    assert_rejected(VALID_PREDICATE + " LIMIT 10", "Rule 6")


def test_rejects_ranking_function_ordered_on_a_non_unique_column() -> None:
    """Ties are the whole subject matter in the duplicate families, so a non-total ordering there
    decides which record survives."""
    assert_rejected(
        VALID_PREDICATE.replace(
            "h.npi          AS offending_value",
            "row_number() OVER (PARTITION BY h.npi ORDER BY h.last_name) AS offending_value",
        ),
        "Rule 6",
    )


def test_accepts_ordering_terminating_in_a_unique_key() -> None:
    validate_predicate(VALID_PREDICATE + " ORDER BY h.valid_from DESC, h.hcp_id LIMIT 10")


# ---------------------------------------------------------------------------
# Rule 7 — master-data joins bind both as-of parameters.
# ---------------------------------------------------------------------------


def test_rejects_master_join_without_watermark() -> None:
    """The most dangerous omission available in this contract.

    The predicate still returns plausible results and still passes the determinism test, which runs
    twice within the same minute — while silently breaking every historical re-run.
    """
    assert_rejected(
        VALID_PREDICATE.replace("AND  h.batch_id   <= :reference_watermark", ""), "Rule 7"
    )


def test_rejects_master_join_without_as_of_date() -> None:
    assert_rejected(VALID_PREDICATE.replace("AND  h.valid_from <= :as_of_date", ""), "Rule 7")


def test_non_master_predicate_needs_no_as_of_binding() -> None:
    """A rule reading only `data_batch` has no version chain to resolve."""
    validate_predicate(
        """
        SELECT b.batch_id::text   AS subject_key,
               NULL::text         AS offending_value,
               b.record_count::text AS observed_value,
               NULL::text         AS expected_value
        FROM   data_batch b
        WHERE  b.batch_id = :batch_id AND b.record_count = 0
        """
    )


# ---------------------------------------------------------------------------
# Rule 8 — declared parameters.
# ---------------------------------------------------------------------------


def test_rejects_undeclared_parameter() -> None:
    assert_rejected(VALID_PREDICATE.replace("'^[0-9]{10}$'", ":some_pattern"), "Rule 8")


def test_engine_parameters_need_no_declaration() -> None:
    validate_predicate(VALID_PREDICATE)


# ---------------------------------------------------------------------------
# T031a — FR-016d, no probabilistic matching.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "levenshtein(h.last_name, 'Smith') < 3",
        "similarity(h.last_name, 'Smith') > 0.8",
        "soundex(h.last_name) = soundex('Smith')",
        "metaphone(h.last_name, 4) = 'SM0'",
        "difference(h.last_name, 'Smith') > 2",
        "word_similarity(h.last_name, 'Smith') > 0.5",
    ],
)
def test_rejects_similarity_functions(expr: str) -> None:
    """FR-016d. Every one of these is IMMUTABLE and passes rule 3, so volatility cannot catch them.

    Duplicate detection is exact match on declared keys. A fuzzy match makes "is this a duplicate?"
    a question of degree, which is precisely what constitution principle I removes from this layer.
    """
    assert_rejected(VALID_PREDICATE.replace("h.npi IS NULL", expr), "FR-016d")


@pytest.mark.parametrize("op", ["h.last_name <-> 'Smith' < 0.3", "h.last_name % 'Smith'"])
def test_rejects_trigram_operators(op: str) -> None:
    assert_rejected(VALID_PREDICATE.replace("h.npi IS NULL", op), "FR-016d")


# ---------------------------------------------------------------------------
# Definition-level validation.
# ---------------------------------------------------------------------------


def test_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        make(threshold=0.2)


@pytest.mark.parametrize("bad_key", ["lower-case", "TRAILING-", "9-LEADING-DIGIT", ""])
def test_rejects_malformed_rule_key(bad_key: str) -> None:
    """A rule key is referenced by findings forever, so its shape is fixed."""
    with pytest.raises(ValidationError):
        make(rule_key=bad_key)


@pytest.mark.parametrize("field", ["domain", "dimension", "severity", "subject_type"])
def test_rejects_value_outside_vocabulary(field: str) -> None:
    with pytest.raises(ValidationError):
        make(**{field: "NOT_A_REAL_VALUE"})


def test_vocabulary_matches_database() -> None:
    """The Literal types here and the CHECK constraints in `dq.domain.dq` must agree.

    A value accepted by Pydantic but rejected by the constraint would fail at INSERT, after the
    registration path had already reported success — the drift is silent until it is not.
    """
    assert set(typing.get_args(Domain)) == set(DOMAINS)
    assert set(typing.get_args(Dimension)) == set(DIMENSIONS)
    assert set(typing.get_args(Severity)) == set(SEVERITIES)
    assert set(typing.get_args(SubjectType)) == set(SUBJECT_TYPES)


def test_semantic_difference_ignores_reformatting() -> None:
    """A reindent is not a version. Otherwise running a formatter over the library appends nine."""
    rule = make()
    reindented = "\n".join("  " + line.strip() for line in VALID_PREDICATE.splitlines())
    assert not rule.differs_semantically_from("HIGH", "record", reindented, {})
    assert rule.differs_semantically_from("MEDIUM", "record", VALID_PREDICATE, {})
