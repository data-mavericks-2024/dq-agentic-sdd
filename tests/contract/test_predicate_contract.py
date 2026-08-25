"""The predicate output contract, and the nine shipped rules against it (T032).

contracts/rule-definition.md fixes four columns, by name: ``subject_key``, ``offending_value``,
``observed_value``, ``expected_value``. The engine's INSERT projects exactly those, so a rule that
returns anything else does not produce a wrong finding — it fails to insert at all.

No database. These tests read the library off disk and check it against the contract, which is what
makes them the fastest possible signal that a rule file is malformed.
"""

from __future__ import annotations

import pytest

from dq.rules.definition import LIBRARY_DIR, RuleDefinition, load_library
from dq.rules.predicates import CONTRACT_COLUMNS, ENGINE_PARAMS, bound_parameter_names

#: The nine rule families FR-015 through FR-022 require. Named explicitly rather than derived from
#: the directory listing: a test that asserts "whatever is on disk is what we expect" cannot notice
#: a rule that was never written.
EXPECTED_RULE_KEYS = {
    "HCP-NPI-FORMAT",
    "HCP-DUP-NPI",
    "HCP-DUP-COMPOSITE",
    "SALES-ORPHAN-REF",
    "SALES-NO-ALIGNMENT",
    "ALIGN-OVERLAP-GAP",
    "SALES-UOM-MISMATCH",
    "FEED-LATE-MISSING",
    "VOL-DEVIATION",
}


@pytest.fixture(scope="module")
def library() -> list[RuleDefinition]:
    return load_library(LIBRARY_DIR)


def test_all_nine_rule_families_are_present(library: list[RuleDefinition]) -> None:
    """SC-001 asserts set equality across nine families. Eight would pass every other test here."""
    assert {r.rule_key for r in library} == EXPECTED_RULE_KEYS


def test_every_rule_loads_and_validates(library: list[RuleDefinition]) -> None:
    """Loading runs the full predicate validator through the Pydantic model validator."""
    assert len(library) == len(EXPECTED_RULE_KEYS)


@pytest.mark.parametrize("column", CONTRACT_COLUMNS)
def test_every_predicate_projects_every_contract_column(
    library: list[RuleDefinition], column: str
) -> None:
    for rule in library:
        assert f"AS {column}" in rule.predicate_sql, (
            f"{rule.rule_key} does not project {column}. The engine's INSERT names all four "
            f"columns, so a missing one fails the statement rather than the rule."
        )


def test_record_rules_never_project_a_null_offending_value(library: list[RuleDefinition]) -> None:
    """SC-002 has a database CHECK behind it: a record-subject finding must carry its value.

    A predicate hard-coding ``NULL AS offending_value`` for a record subject would pass every
    validation rule and then fail at INSERT for the whole scope. Catching it here costs nothing.
    """
    for rule in library:
        if rule.subject_type != "record":
            continue
        assert "NULL::text     AS offending_value" not in rule.predicate_sql, (
            f"{rule.rule_key} is a record rule projecting a literal NULL offending_value; the "
            f"finding CHECK constraint would reject every row it produced."
        )


def test_aggregate_rules_bind_the_scope(library: list[RuleDefinition]) -> None:
    """A source_period rule that ignores the scope evaluates every period on every run.

    The same missing period would then re-fire under a fresh scope_key each time, which is the
    duplication the uniqueness key was rekeyed to prevent (FR-011, research.md D4).
    """
    for rule in library:
        if rule.subject_type != "source_period":
            continue
        bound = bound_parameter_names(rule.predicate_sql)
        assert "scope_source_system_id" in bound, (
            f"{rule.rule_key} is a source_period rule that does not bind "
            f":scope_source_system_id, so it cannot tell which feed it was asked about."
        )


def test_no_rule_binds_an_undeclared_parameter(library: list[RuleDefinition]) -> None:
    """Rule 8, checked across the whole library rather than one predicate at a time."""
    for rule in library:
        undeclared = (
            bound_parameter_names(rule.predicate_sql) - ENGINE_PARAMS - rule.parameters.keys()
        )
        assert not undeclared, f"{rule.rule_key} binds undeclared parameters: {sorted(undeclared)}"


def test_master_joining_rules_bind_both_as_of_parameters(library: list[RuleDefinition]) -> None:
    """Rule 7, stated as a property of the library rather than of one predicate.

    Omitting :reference_watermark returns plausible rows and passes the determinism test, which
    runs twice in the same minute, while silently breaking every historical re-run.
    """
    for rule in library:
        sql = rule.predicate_sql.lower()
        touches_master = any(
            f" {t} " in sql or f" {t}\n" in sql for t in ("hcp", "hco", "product", "territory")
        )
        if not touches_master:
            continue
        bound = bound_parameter_names(rule.predicate_sql)
        assert {"as_of_date", "reference_watermark"} <= bound, (
            f"{rule.rule_key} joins master data but binds only {sorted(bound)}."
        )


def test_severities_and_domains_are_deliberate(library: list[RuleDefinition]) -> None:
    """The composite duplicate rule is MEDIUM on purpose.

    It is evidence of duplication, not proof: two genuine practitioners can share a surname, a first
    initial, a postal code, and a licence state. The spec's edge cases require a seeded namesake pair
    to be an *expected* finding, which only works if the severity says "look at this", not
    "this is broken".
    """
    by_key = {r.rule_key: r for r in library}
    assert by_key["HCP-DUP-COMPOSITE"].severity == "MEDIUM"
    assert by_key["HCP-DUP-NPI"].severity == "HIGH"
    assert by_key["FEED-LATE-MISSING"].subject_type == "source_period"
    assert by_key["VOL-DEVIATION"].parameters["threshold_pct"] == 20
