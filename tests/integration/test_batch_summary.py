"""Batch summary counts reconcile exactly with the underlying findings (T066, SC-004, FR-024,
FR-025).

**Version-drift dedup lives elsewhere, deliberately.** Proving a rule version bump does not double
a summary's counts requires registering a throwaway rule, and any registered rule's findings are
visible to `test_golden_findings.py`'s unscoped, whole-table SC-001 equality check regardless of
which file created them — `test_rule_versioning.py` notes the same hazard about its own
purpose-built rule. This module's own file name sorts *before* `test_golden_findings.py`
alphabetically, so a fixture here that left such a rule active — or even one deactivated too late
— would contaminate that check before it ever ran. `test_summary_version_dedup.py` carries that
scenario instead, named to sort safely after it.

**What this module does not attempt.** T086 owns proving a deliberately failing predicate closes a
run `COMPLETED_WITH_ERRORS`. This module only proves that for the ordinary golden batch, where
nothing errors, `summarise_scope`'s errored-rules line is correctly empty.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, run_rules
from dq.engine.summary import summarise_scope
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")


@pytest.fixture(scope="module")
def evaluated_batch(settings: Settings, seeded: SeedResult, registered: list[str]) -> int:
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status != "FAILED", result.status
    return batch.batch_id


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


def test_errored_rules_are_absent_when_nothing_errored(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    summary = summarise_scope(findings_reader, BatchScope(evaluated_batch))

    assert summary.errored_rules == ()
