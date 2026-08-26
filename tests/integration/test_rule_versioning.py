"""A threshold change appends a version; version 1's findings stay interpretable (T054, SC-005).

The guarantee is not that old findings survive — immutability gives that for free. It is that they
remain **readable as what they meant at the time**. A finding raised under a 3-digit NPI check and
a finding raised under a 10-digit one are different claims, and a steward reading either months
later must be able to tell which question was asked.

That works because ``finding.rule_version_id`` points at an immutable row carrying the exact
predicate, parameters, and severity in force when it was raised. Nothing reconstructs the old rule
from the current one; the old one is still there.

Uses a purpose-built rule rather than one of the nine, so re-registering it cannot disturb the
golden set that ``test_golden_findings.py`` asserts against.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, run_rules
from dq.rules.definition import RuleDefinition
from dq.rules.registry import register, set_active
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

RULE_KEY = "TEST-NPI-LENGTH"

#: Parameterised on the digit count, so a "threshold change" is a parameter change and nothing else.
PREDICATE = """
SELECT h.hcp_id::text                     AS subject_key,
       coalesce(h.npi, '<absent>')        AS offending_value,
       coalesce(length(h.npi), 0)::text   AS observed_value,
       CAST(:digits AS text)              AS expected_value
FROM   hcp h
WHERE  h.batch_id    =  :batch_id
  AND  h.valid_from <=  :as_of_date
  AND  h.batch_id   <=  :reference_watermark
  AND  NOT h.is_deleted
  AND  coalesce(length(h.npi), 0) <> :digits
"""


def definition(digits: int, severity: str = "MEDIUM") -> RuleDefinition:
    return RuleDefinition.model_validate(
        {
            "rule_key": RULE_KEY,
            "domain": "HCP",
            "dimension": "validity",
            "severity": severity,
            "owning_function": "Master Data Management",
            "subject_type": "record",
            "predicate_sql": PREDICATE,
            "parameters": {"digits": digits},
        }
    )


def _register(settings: Settings, defn: RuleDefinition) -> Any:
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        return register(conn, defn)


@pytest.fixture(scope="module")
def versioned(
    settings: Settings, seeded: SeedResult, registered: list[str]
) -> Iterator[dict[str, Any]]:
    """Register v1, run, change the threshold, register again, run again.

    **Deactivated on teardown.** This rule is not one of the nine, so leaving it active would add
    findings to any later full run and break SC-001's set equality in
    ``test_golden_findings.py``. Today that file happens to run first, alphabetically — relying on
    that would make the golden test's correctness depend on a filename.

    Deactivating rather than deleting, because ``rule_version`` and ``finding`` are immutable by
    trigger: the rows this module creates cannot be removed, and should not be. They are a true
    record of what ran.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    v1 = _register(settings, definition(digits=10))
    run_rules(settings, BatchScope(batch.batch_id), rule_keys=[RULE_KEY])

    v2 = _register(settings, definition(digits=9))
    run_rules(settings, BatchScope(batch.batch_id), rule_keys=[RULE_KEY])

    yield {"batch_id": batch.batch_id, "v1": v1, "v2": v2}

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        set_active(conn, RULE_KEY, False)


def test_the_threshold_change_created_a_second_version(versioned: dict[str, Any]) -> None:
    assert versioned["v1"].outcome == "created"
    assert versioned["v1"].version_no == 1
    assert versioned["v2"].outcome == "versioned"
    assert versioned["v2"].version_no == 2
    assert versioned["v2"].rule_id == versioned["v1"].rule_id, "a version must stay on its rule"
    assert versioned["v2"].rule_version_id != versioned["v1"].rule_version_id


def test_re_registering_the_same_definition_is_a_no_op(
    settings: Settings, versioned: dict[str, Any]
) -> None:
    """What makes `dq rules register --all` safe to run on every deploy."""
    again = _register(settings, definition(digits=9))
    assert again.outcome == "unchanged"
    assert again.version_no == 2


def test_version_one_is_still_readable_in_full(
    findings_reader: Connection, versioned: dict[str, Any]
) -> None:
    """SC-005. The old version is *present*, not reconstructed.

    Its predicate, parameters and severity are exactly what they were — which is the only way a
    finding raised under it can still be explained.
    """
    row = findings_reader.execute(
        text(
            "SELECT version_no, severity, subject_type, predicate_sql, parameters "
            "FROM rule_version WHERE rule_version_id = :id"
        ),
        {"id": versioned["v1"].rule_version_id},
    ).one()

    assert row.version_no == 1
    assert row.parameters == {"digits": 10}, "version 1 must still carry its own threshold"
    assert row.severity == "MEDIUM"
    assert "coalesce(length(h.npi), 0) <> :digits" in row.predicate_sql


def test_findings_from_both_versions_coexist_and_are_attributable(
    findings_reader: Connection, versioned: dict[str, Any]
) -> None:
    """Each finding names the version that produced it.

    The uniqueness key is ``(rule_version_id, scope_key, subject_key)``, so the same subject failing
    under both versions yields two rows — correctly. They are two different claims about the same
    record, made by two different questions.
    """
    rows = findings_reader.execute(
        text(
            "SELECT rv.version_no, count(*) AS n "
            "FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :k AND f.scope_key = :scope "
            "GROUP BY rv.version_no ORDER BY rv.version_no"
        ),
        {"k": RULE_KEY, "scope": f"b:{versioned['batch_id']}"},
    ).all()

    by_version = {r.version_no: r.n for r in rows}
    assert 1 in by_version, "version 1's findings were lost"
    assert 2 in by_version, "version 2 produced nothing, so there is nothing to compare"
    assert by_version[1] > 0 and by_version[2] > 0


def test_a_findings_expected_value_reflects_its_own_version(
    findings_reader: Connection, versioned: dict[str, Any]
) -> None:
    """The strongest form of "still interpretable": the finding carries the threshold it was judged
    against, not the one in force today."""
    rows = findings_reader.execute(
        text(
            "SELECT DISTINCT rv.version_no, f.expected_value "
            "FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :k ORDER BY rv.version_no"
        ),
        {"k": RULE_KEY},
    ).all()

    expected = {r.version_no: r.expected_value for r in rows}
    assert expected[1] == "10", "a version-1 finding should still say it wanted 10 digits"
    assert expected[2] == "9"


def test_only_the_newest_version_is_evaluated_on_a_later_run(
    settings: Settings, findings_reader: Connection, versioned: dict[str, Any]
) -> None:
    """A rule with two versions is one check that changed, not two checks.

    Evaluating both would raise two findings for one problem, and the count would grow with every
    threshold revision.
    """
    result = run_rules(settings, BatchScope(versioned["batch_id"]), rule_keys=[RULE_KEY])
    assert len(result.outcomes) == 1
    assert result.outcomes[0].rule_version_id == versioned["v2"].rule_version_id
