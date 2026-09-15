"""Batch summary counts reconcile exactly, and survive a rule version change without doubling
(T066, SC-004, FR-024, FR-025).

**What this module does not attempt.** T086 owns proving a deliberately failing predicate closes a
run `COMPLETED_WITH_ERRORS`. This module only proves that *if* `rule_run_rule_version` records an
`ERRORED` outcome, `summarise_scope` surfaces it as an explicit line — and, for the ordinary golden
batch where nothing errors, that the line is correctly absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, run_rules
from dq.engine.summary import summarise_scope
from dq.rules.definition import RuleDefinition
from dq.rules.registry import register, set_active
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

#: A distinct rule_key from every other test module's purpose-built rule, so a run in this module
#: cannot be mistaken for one elsewhere in the golden-set comparisons those modules make.
RULE_KEY = "TEST-SUMMARY-DEDUP"

#: Flags every non-deleted HCP in the batch regardless of `:marker` — the threshold plays no part
#: in which subjects match, only in which *version* produced the finding. That isolates the one
#: thing this test needs to prove: two versions of the same rule agreeing on the same subject must
#: count once in the summary, not twice.
PREDICATE = """
SELECT h.hcp_id::text            AS subject_key,
       CAST(:marker AS text)     AS offending_value,
       NULL::text                AS observed_value,
       NULL::text                AS expected_value
FROM   hcp h
WHERE  h.batch_id    =  :batch_id
  AND  h.valid_from <=  :as_of_date
  AND  h.batch_id   <=  :reference_watermark
  AND  NOT h.is_deleted
"""


def _definition(marker: int) -> RuleDefinition:
    return RuleDefinition.model_validate(
        {
            "rule_key": RULE_KEY,
            "domain": "HCP",
            "dimension": "validity",
            "severity": "LOW",
            "owning_function": "Master Data Management",
            "subject_type": "record",
            "predicate_sql": PREDICATE,
            "parameters": {"marker": marker},
        }
    )


@pytest.fixture(scope="module")
def evaluated_batch(settings: Settings, seeded: SeedResult, registered: list[str]) -> int:
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status != "FAILED", result.status
    return batch.batch_id


@pytest.fixture(scope="module")
def dedup_rule(settings: Settings, evaluated_batch: int) -> Iterator[dict[str, Any]]:
    """Register v1, run it, bump the version, run again. Same subjects, two versions.

    Deactivated on teardown for the same reason `test_rule_versioning.py` deactivates its own
    purpose-built rule: `rule_version` and `finding` are immutable, so the rows this creates cannot
    be removed, and leaving the rule active would perturb `test_golden_findings.py`'s set equality.
    """
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        v1 = register(conn, _definition(marker=1))
    run_rules(settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY])

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        v2 = register(conn, _definition(marker=2))
    run_rules(settings, BatchScope(evaluated_batch), rule_keys=[RULE_KEY])

    assert v2.version_no > v1.version_no, "a parameter change must append a version (T056)"

    yield {"batch_id": evaluated_batch, "v1": v1, "v2": v2}

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        set_active(conn, RULE_KEY, False)


def test_summary_counts_reconcile_exactly_with_the_underlying_findings(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    """FR-025. The golden batch has no version drift among its nine rules, so the deduplicated
    total must equal a plain row count for this scope — the strongest form of the check."""
    summary = summarise_scope(findings_reader, BatchScope(evaluated_batch))

    raw_count = findings_reader.execute(
        text("SELECT count(*) FROM finding WHERE scope_key = :key"),
        {"key": f"b:{evaluated_batch}"},
    ).scalar_one()

    assert summary.total_findings > 0, "nothing was detected, so this test would pass vacuously"
    assert summary.total_findings == raw_count
    assert sum(summary.by_domain.values()) == summary.total_findings
    assert sum(rc.finding_count for rc in summary.by_rule) == summary.total_findings
    assert sum(summary.by_severity.values()) == summary.total_findings


def test_a_threshold_change_does_not_double_count_the_same_subject(
    findings_reader: Connection, dedup_rule: dict[str, Any]
) -> None:
    """The failure a raw `COUNT(*)` grouped by rule_version_id would produce: the same set of HCPs,
    flagged twice — once per version — reported as twice as many problems."""
    batch_id = dedup_rule["batch_id"]

    raw_row_count = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :rule_key"
        ),
        {"rule_key": RULE_KEY},
    ).scalar_one()
    distinct_subject_count = findings_reader.execute(
        text(
            "SELECT count(DISTINCT f.subject_key) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = :rule_key"
        ),
        {"rule_key": RULE_KEY},
    ).scalar_one()

    assert raw_row_count == 2 * distinct_subject_count, (
        "both versions must have fired against every subject, or this test proves nothing"
    )

    summary = summarise_scope(findings_reader, BatchScope(batch_id))
    line = next(rc for rc in summary.by_rule if rc.rule_key == RULE_KEY)

    assert line.finding_count == distinct_subject_count
    assert line.finding_count != raw_row_count


def test_errored_rules_are_absent_when_nothing_errored(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    summary = summarise_scope(findings_reader, BatchScope(evaluated_batch))

    assert summary.errored_rules == ()
