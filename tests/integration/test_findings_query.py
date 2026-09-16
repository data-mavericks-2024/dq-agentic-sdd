"""Findings queryable by domain, rule, severity, batch, and time window, in any combination
(T065, FR-023, US1/AC4, SC-002).

Reuses the golden batch `test_golden_findings.py` already proves is correct, rather than seeding a
second scenario — this module is about the query surface, not about re-litigating detection.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Connection

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, run_rules
from dq.engine.summary import query_findings
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")


@pytest.fixture(scope="module")
def evaluated_batch(settings: Settings, seeded: SeedResult, registered: list[str]) -> int:
    """The target batch, evaluated. Returns its `batch_id`."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status != "FAILED", result.status
    return batch.batch_id


def test_no_filters_returns_every_finding_for_the_batch(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    all_findings = query_findings(findings_reader)
    matching_batch = [f for f in all_findings if f.subject_type == "record"]

    assert len(all_findings) > 0, "nothing was detected, so this test would pass vacuously"
    # Every record-subject finding is fully attributable without a separate lookup (US1/AC4).
    for f in matching_batch:
        assert f.rule_key
        assert f.domain
        assert f.dimension
        assert f.owning_function
        assert f.severity
        assert f.offending_value is not None, "SC-002 — a record finding must carry its offense"


def test_batch_filter_narrows_to_that_batch_only(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    all_findings = query_findings(findings_reader)
    by_batch = query_findings(findings_reader, batch_id=evaluated_batch)

    assert 0 < len(by_batch) <= len(all_findings)
    # A batch scope evaluates only record- and batch-subject rules (`ResolvedScope.subject_types`
    # in src/dq/engine/runner.py) — a `source_period` finding could never carry this batch_id.
    assert all(f.subject_type in ("record", "batch") for f in by_batch)


def test_domain_filter_returns_only_that_domain(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    results = query_findings(findings_reader, domain="HCP")

    assert len(results) > 0, "the golden set injects HCP defects; an empty result is a real failure"
    assert all(f.domain == "HCP" for f in results)


def test_rule_filter_returns_only_that_rule(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    results = query_findings(findings_reader, rule_key="HCP-NPI-FORMAT")

    assert len(results) > 0
    assert all(f.rule_key == "HCP-NPI-FORMAT" for f in results)


def test_severity_filter_returns_only_that_severity(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    results = query_findings(findings_reader, severity="HIGH")

    assert len(results) > 0
    assert all(f.severity == "HIGH" for f in results)


def test_combined_filters_are_anded_not_ored(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    """Domain HCP and severity MEDIUM together must be narrower than either alone — the failure a
    naive OR implementation would produce."""
    hcp_only = query_findings(findings_reader, domain="HCP")
    medium_only = query_findings(findings_reader, severity="MEDIUM")
    combined = query_findings(findings_reader, domain="HCP", severity="MEDIUM")

    assert len(combined) <= len(hcp_only)
    assert len(combined) <= len(medium_only)
    assert all(f.domain == "HCP" and f.severity == "MEDIUM" for f in combined)


def test_time_window_excludes_findings_outside_it(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    all_findings = query_findings(findings_reader)
    assert len(all_findings) > 0
    earliest = min(f.detected_at for f in all_findings)

    before_anything = query_findings(findings_reader, detected_to=earliest)
    after_everything = query_findings(findings_reader, detected_from=earliest)

    assert before_anything == []
    assert len(after_everything) == len(all_findings)


def test_a_filter_matching_nothing_returns_an_empty_list_not_an_error(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    results = query_findings(findings_reader, rule_key="NO-SUCH-RULE-KEY")

    assert results == []


def test_future_time_window_returns_nothing(
    findings_reader: Connection, evaluated_batch: int
) -> None:
    all_findings = query_findings(findings_reader)
    latest = max(f.detected_at for f in all_findings)

    results = query_findings(findings_reader, detected_from=latest + timedelta(days=1))

    assert results == []
