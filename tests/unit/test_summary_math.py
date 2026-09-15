"""Summary aggregation arithmetic (T067).

Pure Python over `_DistinctFinding` tuples — no database. The SQL in `src/dq/engine/summary.py`
does the deduplication (`DISTINCT ON (rule_id, subject_key)`); this only checks the arithmetic that
runs after it. That split is deliberate: `test_summary_aggregates.py` proves the dedup query itself
is right, and this proves the counting is right given correctly-deduplicated input — a bug in either
half should fail exactly one of the two suites, not both at once.
"""

from __future__ import annotations

from dq.engine.summary import RuleCount, _aggregate, _DistinctFinding


def _row(rule_id: int, rule_key: str, domain: str, subject_key: str, severity: str) -> _DistinctFinding:
    return _DistinctFinding(
        rule_id=rule_id, rule_key=rule_key, domain=domain, subject_key=subject_key, severity=severity
    )


def test_empty_input_produces_empty_and_reconciling_summary() -> None:
    by_domain, by_rule, by_severity = _aggregate([])

    assert by_domain == {}
    assert by_rule == ()
    assert by_severity == {}


def test_three_groupings_always_sum_to_the_total() -> None:
    """FR-025 — summary counts must reconcile exactly with the findings they summarise.

    Domain, rule, and severity partition the same deduplicated row set three different ways, so
    however the rows are sliced, each grouping's counts must sum back to the total row count.
    """
    rows = [
        _row(1, "HCP-NPI-FORMAT", "HCP", "hcp-1", "HIGH"),
        _row(1, "HCP-NPI-FORMAT", "HCP", "hcp-2", "HIGH"),
        _row(2, "HCP-DUP-NPI", "HCP", "hcp-3", "HIGH"),
        _row(3, "SALES-ORPHAN-REF", "SALES", "sale-9", "MEDIUM"),
    ]

    by_domain, by_rule, by_severity = _aggregate(rows)

    assert sum(by_domain.values()) == len(rows)
    assert sum(rc.finding_count for rc in by_rule) == len(rows)
    assert sum(by_severity.values()) == len(rows)


def test_grouping_values_are_correct_not_only_reconciling() -> None:
    """Summing to the total is necessary but not sufficient — a bug that moved rows between
    buckets while preserving the total would pass the reconciliation test above and still be wrong.
    """
    rows = [
        _row(1, "HCP-NPI-FORMAT", "HCP", "hcp-1", "HIGH"),
        _row(1, "HCP-NPI-FORMAT", "HCP", "hcp-2", "HIGH"),
        _row(2, "HCP-DUP-NPI", "HCP", "hcp-3", "MEDIUM"),
        _row(3, "SALES-ORPHAN-REF", "SALES", "sale-9", "MEDIUM"),
    ]

    by_domain, by_rule, by_severity = _aggregate(rows)

    assert by_domain == {"HCP": 3, "SALES": 1}
    assert by_severity == {"HIGH": 2, "MEDIUM": 2}
    assert set(by_rule) == {
        RuleCount(rule_key="HCP-NPI-FORMAT", domain="HCP", finding_count=2),
        RuleCount(rule_key="HCP-DUP-NPI", domain="HCP", finding_count=1),
        RuleCount(rule_key="SALES-ORPHAN-REF", domain="SALES", finding_count=1),
    }


def test_by_rule_is_sorted_by_rule_key() -> None:
    """Deterministic output ordering, not incidental dict order, so a CLI or API caller need not
    re-sort what is already meant to be a stable report."""
    rows = [
        _row(3, "Z-RULE", "SALES", "s-1", "LOW"),
        _row(1, "A-RULE", "HCP", "h-1", "HIGH"),
        _row(2, "M-RULE", "HCP", "h-2", "MEDIUM"),
    ]

    _, by_rule, _ = _aggregate(rows)

    assert [rc.rule_key for rc in by_rule] == ["A-RULE", "M-RULE", "Z-RULE"]


def test_two_rules_flagging_the_same_subject_both_count() -> None:
    """Two distinct rules agreeing a record is bad are two findings, not one — the dedup this
    module performs collapses *version* duplication for the same rule, never distinct rules."""
    rows = [
        _row(1, "HCP-NPI-FORMAT", "HCP", "hcp-1", "HIGH"),
        _row(2, "HCP-DUP-NPI", "HCP", "hcp-1", "HIGH"),
    ]

    by_domain, by_rule, _ = _aggregate(rows)

    assert by_domain == {"HCP": 2}
    assert {rc.finding_count for rc in by_rule} == {1, 1}
