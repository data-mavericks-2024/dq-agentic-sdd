"""A rule that errors is recorded as ERRORED, other rules still run, and the run closes
COMPLETED_WITH_ERRORS rather than COMPLETED (T086, FR-014, spec Edge Cases).

**Explicitly `rule_keys`-scoped, never a bare full-batch run.** The broken rule stays *active*
between registration and this module's teardown — the same window `test_rule_versioning.py` and
`test_summary_version_dedup.py` accept for their own purpose-built rules. Several other modules
(`test_crash_resume.py`, `test_reproducibility.py`, `test_rule_extensibility.py`, …) call
`run_rules(settings, BatchScope(...))` with no `rule_keys` filter and assert `status == "COMPLETED"`.
If this rule were active and record-subject during one of those calls, their batch would pick it up
and their status would read `COMPLETED_WITH_ERRORS` instead — a real rule genuinely evaluates every
active rule matching its subject types, so an unfiltered call elsewhere is not a bug to route around,
it is the correct behavior colliding with a fixture that outlives its own test. Scoping every call
here to an explicit `rule_keys` list is what keeps this module's broken rule from ever being invoked
by a bare batch run it is not testing, in this module or any other.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, run_rules
from dq.rules.definition import RuleDefinition
from dq.rules.registry import register, set_active
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

RULE_KEY = "TEST-DELIBERATE-ERROR"

#: Division by zero, on real per-row data rather than a literal `1/0` — the failure mode
#: `_evaluate_one`'s own comment names as an anticipated case. `h.hcp_id - h.hcp_id` is zero for
#: every row but is not a constant the planner folds away, so the error surfaces only when the
#: predicate actually executes against a batch that has at least one matching row.
PREDICATE = """
SELECT h.hcp_id::text                       AS subject_key,
       'error-probe'::text                  AS offending_value,
       (1 / (h.hcp_id - h.hcp_id))::text    AS observed_value,
       NULL::text                            AS expected_value
FROM   hcp h
WHERE  h.batch_id    =  :batch_id
  AND  h.valid_from <=  :as_of_date
  AND  h.batch_id   <=  :reference_watermark
  AND  NOT h.is_deleted
"""


def _definition() -> RuleDefinition:
    return RuleDefinition.model_validate(
        {
            "rule_key": RULE_KEY,
            "domain": "HCP",
            "dimension": "validity",
            "severity": "LOW",
            "owning_function": "Master Data Management",
            "subject_type": "record",
            "predicate_sql": PREDICATE,
            "parameters": {},
        }
    )


@pytest.fixture(scope="module")
def evaluated_batch(settings: Settings, seeded: SeedResult, registered: list[str]) -> int:
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status != "FAILED", result.status
    return batch.batch_id


@pytest.fixture(scope="module")
def broken_rule(settings: Settings, evaluated_batch: int) -> Iterator[None]:
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        register(conn, _definition())

    yield

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        set_active(conn, RULE_KEY, False)


def test_the_run_closes_completed_with_errors_not_completed(
    settings: Settings, evaluated_batch: int, broken_rule: None
) -> None:
    result = run_rules(
        settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY, "HCP-NPI-FORMAT"]
    )

    assert result.status == "COMPLETED_WITH_ERRORS", result.status


def test_the_broken_rule_is_recorded_as_errored_with_a_detail(
    settings: Settings, evaluated_batch: int, broken_rule: None
) -> None:
    result = run_rules(
        settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY, "HCP-NPI-FORMAT"]
    )

    errored = [o for o in result.outcomes if o.rule_key == RULE_KEY]
    assert len(errored) == 1
    assert errored[0].outcome == "ERRORED"
    assert errored[0].finding_count == 0
    assert errored[0].error_detail is not None
    assert "division by zero" in errored[0].error_detail.lower()


def test_the_other_rule_in_the_same_run_still_evaluates(
    settings: Settings, evaluated_batch: int, broken_rule: None
) -> None:
    """Per-rule granularity, not per-run: one rule failing must not stop its neighbours (FR-014)."""
    result = run_rules(
        settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY, "HCP-NPI-FORMAT"]
    )

    survivor = next(o for o in result.outcomes if o.rule_key == "HCP-NPI-FORMAT")
    assert survivor.outcome == "EVALUATED"


def test_the_errored_rule_contributes_no_finding_rows(
    findings_reader: Connection, settings: Settings, evaluated_batch: int, broken_rule: None
) -> None:
    """A statement that errors is rolled back to its savepoint entirely — not some rows in, some
    out. Zero rows for a rule that never completed is the only coherent count."""
    run_rules(settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY, "HCP-NPI-FORMAT"])

    count = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :rule_key"
        ),
        {"rule_key": RULE_KEY},
    ).scalar_one()

    assert count == 0
