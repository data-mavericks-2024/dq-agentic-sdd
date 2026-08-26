"""Which field changes create a rule version, and which do not (T053, FR-006).

The rule is simple to state and easy to get wrong in the permissive direction: **a version records a
change in what a finding means.** Four fields carry meaning, two do not.

Getting it wrong either way has a cost, and they are not symmetric:

* **Versioning too eagerly** — appending on ``owning_function`` or on a reindent — buries the real
  threshold changes in noise. A steward reading "version 14" cannot tell whether the check changed
  or someone reformatted the YAML, so the version number stops carrying information.
* **Versioning too rarely** — editing a predicate in place — is worse and is silent. Findings
  recorded under the old meaning would now be read under the new one, and nothing anywhere would
  show that the question had changed. That is the failure SC-005 exists to prevent, and no test
  downstream of it would notice.

So this file is exhaustive over the field list rather than illustrative.
"""

from __future__ import annotations

from typing import Any

import pytest

from dq.rules.definition import VERSIONED_FIELDS, RuleDefinition

PREDICATE = """
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

BASE: dict[str, Any] = {
    "rule_key": "TEST-VERSIONING",
    "domain": "HCP",
    "dimension": "validity",
    "severity": "HIGH",
    "owning_function": "Master Data Management",
    "subject_type": "record",
    "predicate_sql": PREDICATE,
    "parameters": {},
}


def make(**overrides: Any) -> RuleDefinition:
    return RuleDefinition.model_validate({**BASE, **overrides})


def stored_from(definition: RuleDefinition) -> dict[str, Any]:
    """The four stored values ``differs_semantically_from`` compares against."""
    return {
        "severity": definition.severity,
        "subject_type": definition.subject_type,
        "predicate_sql": definition.predicate_sql,
        "parameters": definition.parameters,
    }


# ---------------------------------------------------------------------------
# The field list itself, so a new field cannot be added without a decision.
# ---------------------------------------------------------------------------


def test_the_versioned_field_list_is_exactly_these_four() -> None:
    """Pinned deliberately.

    Adding a field to ``RuleDefinition`` forces a choice here: does changing it alter what a finding
    means? Leaving the list alone answers "no" by default, and a default answer to that question is
    how a predicate change eventually slips through unversioned.
    """
    assert set(VERSIONED_FIELDS) == {
        "predicate_sql",
        "parameters",
        "severity",
        "subject_type",
    }


def test_every_versioned_field_is_a_real_field() -> None:
    fields = set(RuleDefinition.model_fields)
    assert set(VERSIONED_FIELDS) <= fields


# ---------------------------------------------------------------------------
# Changes that MUST version.
# ---------------------------------------------------------------------------


def test_a_predicate_change_versions() -> None:
    original = make()
    changed = make(predicate_sql=PREDICATE.replace("!~ '^[0-9]{10}$'", "!~ '^[0-9]{9}$'"))
    assert changed.differs_semantically_from(**stored_from(original))


def test_a_severity_change_versions() -> None:
    """Severity is versioned because it changes what a finding *means* to a steward.

    A finding recorded when the rule was HIGH was triaged as HIGH. Re-reading it under a later
    MEDIUM would misrepresent the decision that was made about it.
    """
    original = make()
    assert make(severity="MEDIUM").differs_semantically_from(**stored_from(original))


def test_a_subject_type_change_versions() -> None:
    """The subject type decides what the finding points at — a row, or a period with no row."""
    original = make()
    aggregate = make(
        subject_type="source_period",
        predicate_sql="""
        SELECT s.code AS subject_key,
               NULL::text AS offending_value,
               NULL::text AS observed_value,
               NULL::text AS expected_value
        FROM   source_system s
        WHERE  s.source_system_id = :scope_source_system_id
        """,
    )
    assert aggregate.differs_semantically_from(**stored_from(original))


def test_a_parameter_value_change_versions() -> None:
    """The threshold case SC-005 names directly.

    A rule whose threshold moves from 20% to 45% is asking a different question. Findings raised
    under the old threshold must stay attributable to it, or "why was this flagged?" has no answer.
    """
    parameterised = make(
        predicate_sql=PREDICATE.replace("'^[0-9]{10}$'", ":pattern"),
        parameters={"pattern": "^[0-9]{10}$"},
    )
    retightened = make(
        predicate_sql=PREDICATE.replace("'^[0-9]{10}$'", ":pattern"),
        parameters={"pattern": "^[0-9]{9}$"},
    )
    assert retightened.differs_semantically_from(**stored_from(parameterised))


def test_adding_a_parameter_versions() -> None:
    original = make()
    with_param = make(
        predicate_sql=PREDICATE.replace("'^[0-9]{10}$'", ":pattern"),
        parameters={"pattern": "^[0-9]{10}$"},
    )
    assert with_param.differs_semantically_from(**stored_from(original))


# ---------------------------------------------------------------------------
# Changes that MUST NOT version.
# ---------------------------------------------------------------------------


def test_an_owning_function_change_does_not_version() -> None:
    """Ownership is who to call, not what was checked.

    A team reorganisation would otherwise append a version to every rule it touched, and the version
    history would stop being a record of changed meaning.
    """
    original = make()
    reassigned = make(owning_function="Commercial Operations")
    assert not reassigned.differs_semantically_from(**stored_from(original))


def test_reformatting_the_predicate_does_not_version() -> None:
    """Whitespace is not meaning.

    Without this, running a formatter over the rule library appends a version to all nine at once —
    and the next real threshold change is indistinguishable from that noise.
    """
    original = make()
    reindented = make(
        predicate_sql="\n".join("    " + line.strip() for line in PREDICATE.splitlines())
    )
    assert not reindented.differs_semantically_from(**stored_from(original))


def test_collapsing_predicate_whitespace_does_not_version() -> None:
    original = make()
    one_line = make(predicate_sql=" ".join(PREDICATE.split()))
    assert not one_line.differs_semantically_from(**stored_from(original))


def test_an_identical_definition_does_not_version() -> None:
    """Re-registering an unchanged rule is a no-op, which is what makes `--all` safe to re-run."""
    original = make()
    assert not make().differs_semantically_from(**stored_from(original))


def test_parameter_ordering_does_not_version() -> None:
    """`parameters` is a mapping. Two mappings with the same pairs are the same declaration."""
    a = make(
        predicate_sql=PREDICATE.replace("'^[0-9]{10}$'", ":pattern"),
        parameters={"pattern": "^[0-9]{10}$", "unused_note": "x"},
    )
    b = make(
        predicate_sql=PREDICATE.replace("'^[0-9]{10}$'", ":pattern"),
        parameters={"unused_note": "x", "pattern": "^[0-9]{10}$"},
    )
    assert not b.differs_semantically_from(**stored_from(a))


# ---------------------------------------------------------------------------
# The classification, stated as a table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "new_value", "should_version"),
    [
        ("severity", "LOW", True),
        ("owning_function", "Someone Else", False),
    ],
)
def test_classification_table(field: str, new_value: Any, should_version: bool) -> None:
    original = make()
    changed = make(**{field: new_value})
    assert changed.differs_semantically_from(**stored_from(original)) is should_version, (
        f"changing {field!r} should {'' if should_version else 'not '}create a version"
    )
