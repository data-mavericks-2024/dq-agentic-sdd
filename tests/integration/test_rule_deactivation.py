"""A deactivated rule produces no new findings and loses none historically (T055, SC-006, FR-007).

Retiring a rule is the operation most likely to be implemented as a delete, and a delete is exactly
wrong here. Findings raised while the rule was active are still true statements about what was
checked and what failed. Removing the rule would leave them citing something unreadable, and the
audit trail would have a hole shaped like a decision someone made.

So deactivation touches one boolean on ``rule`` and nothing else. ``rule_version`` stays, the
findings stay, and the only change is that the engine stops selecting it.

Note what is *not* versioned: ``is_active`` is not a versioned field, because switching a rule off
does not change what its past findings meant. Versioning on it would append a row every time
someone paused a noisy rule for an afternoon.
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

RULE_KEY = "TEST-DEACTIVATION"

PREDICATE = """
SELECT h.hcp_id::text        AS subject_key,
       h.source_key          AS offending_value,
       NULL::text            AS observed_value,
       NULL::text            AS expected_value
FROM   hcp h
WHERE  h.batch_id    =  :batch_id
  AND  h.valid_from <=  :as_of_date
  AND  h.batch_id   <=  :reference_watermark
  AND  NOT h.is_deleted
"""

DEFINITION = RuleDefinition.model_validate(
    {
        "rule_key": RULE_KEY,
        "domain": "HCP",
        "dimension": "completeness",
        "severity": "LOW",
        "owning_function": "Master Data Management",
        "subject_type": "record",
        "predicate_sql": PREDICATE,
        "parameters": {},
    }
)


def _set_active(settings: Settings, active: bool) -> bool:
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        return set_active(conn, RULE_KEY, active)


@pytest.fixture(scope="module")
def deactivated(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> Iterator[dict[str, Any]]:
    """Register, run, count, deactivate. Left deactivated — which is also the cleanup."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        registration = register(conn, DEFINITION)

    run_rules(settings, BatchScope(batch.batch_id), rule_keys=[RULE_KEY])

    before = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "WHERE rv.rule_version_id = :v"
        ),
        {"v": registration.rule_version_id},
    ).scalar_one()

    assert _set_active(settings, False), "deactivation reported no rule matched"

    yield {
        "batch_id": batch.batch_id,
        "registration": registration,
        "findings_before": before,
    }


def test_the_rule_produced_findings_while_active(deactivated: dict[str, Any]) -> None:
    """Without this, every assertion below would hold for a rule that never worked."""
    assert deactivated["findings_before"] > 0


def test_a_deactivated_rule_is_not_evaluated(
    settings: Settings, deactivated: dict[str, Any]
) -> None:
    """The engine selects on ``rule.is_active``, so the rule simply is not in the run."""
    result = run_rules(settings, BatchScope(deactivated["batch_id"]))
    evaluated = {o.rule_key for o in result.outcomes}
    assert RULE_KEY not in evaluated


def test_asking_for_it_by_name_still_does_not_evaluate_it(
    settings: Settings, deactivated: dict[str, Any]
) -> None:
    """`--rule` narrows the selection; it does not override deactivation.

    Otherwise "switched off" would mean "switched off unless someone asks", and a retired rule could
    quietly start producing findings again from a scheduled job that named it explicitly.
    """
    result = run_rules(settings, BatchScope(deactivated["batch_id"]), rule_keys=[RULE_KEY])
    assert result.outcomes == []


def test_no_new_findings_appear_after_deactivation(
    settings: Settings, findings_reader: Connection, deactivated: dict[str, Any]
) -> None:
    """SC-006, first half."""
    run_rules(settings, BatchScope(deactivated["batch_id"]))

    after = findings_reader.execute(
        text("SELECT count(*) FROM finding WHERE rule_version_id = :v"),
        {"v": deactivated["registration"].rule_version_id},
    ).scalar_one()

    assert after == deactivated["findings_before"]


def test_historical_findings_are_all_still_present(
    findings_reader: Connection, deactivated: dict[str, Any]
) -> None:
    """SC-006, second half — the half a delete would break."""
    after = findings_reader.execute(
        text("SELECT count(*) FROM finding WHERE rule_version_id = :v"),
        {"v": deactivated["registration"].rule_version_id},
    ).scalar_one()
    assert after == deactivated["findings_before"]


def test_the_rule_and_its_version_are_still_readable(
    findings_reader: Connection, deactivated: dict[str, Any]
) -> None:
    """A finding cites a rule version. If deactivation removed it, the citation would dangle."""
    row = findings_reader.execute(
        text(
            "SELECT r.rule_key, r.is_active, rv.version_no, rv.predicate_sql "
            "FROM rule_version rv JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE rv.rule_version_id = :v"
        ),
        {"v": deactivated["registration"].rule_version_id},
    ).one()

    assert row.rule_key == RULE_KEY
    assert row.is_active is False
    assert row.version_no == 1
    assert row.predicate_sql.strip(), "the predicate must still be readable after retirement"


def test_deactivation_did_not_append_a_version(
    findings_reader: Connection, deactivated: dict[str, Any]
) -> None:
    """`is_active` is not a versioned field — switching a rule off changes no past meaning."""
    versions = findings_reader.execute(
        text(
            "SELECT count(*) FROM rule_version rv JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :k"
        ),
        {"k": RULE_KEY},
    ).scalar_one()
    assert versions == 1


def test_reactivation_restores_evaluation(settings: Settings, deactivated: dict[str, Any]) -> None:
    """Retirement is reversible, and reversing it does not create a version either."""
    assert _set_active(settings, True)
    try:
        result = run_rules(settings, BatchScope(deactivated["batch_id"]), rule_keys=[RULE_KEY])
        assert len(result.outcomes) == 1
        assert result.outcomes[0].rule_key == RULE_KEY
    finally:
        _set_active(settings, False)


def test_deactivating_an_unknown_rule_reports_it(settings: Settings) -> None:
    """A silent no-op here would let a typo in a retirement script look like success."""
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        assert set_active(conn, "NO-SUCH-RULE", False) is False
