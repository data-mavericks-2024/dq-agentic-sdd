"""Findings query and scope summarisation (T068, T069).

Two read paths sit on top of the same `finding` table the runner writes. Both connect as
**`dq_readonly`** — neither writes anything, and if a steward's query can be answered through the
investigation role, that is evidence the grant matrix is sufficient rather than an assumption about
it (the same reasoning `findings_reader` in `tests/conftest.py` already relies on for assertions).

**Why counting is not `COUNT(*)` (FR-024, FR-025).** A rule's `severity` and `subject_type` are
versioned; `domain`, `dimension`, and `owning_function` are not — they live on `rule`, not
`rule_version` (`src/dq/domain/dq.py`). A threshold change appends a `rule_version` and a re-run
re-evaluates the same subject under the new version, which inserts a *second* finding row keyed on
the new `rule_version_id` — same subject, same scope, same rule. A raw `COUNT(*)` would silently
turn one threshold change into "twice as many problems." Every grouped count here is instead over
distinct `(rule_id, subject_key)` pairs, with the newest version's row canonical when a subject is
still flagged today but was flagged differently under an earlier version — the newest version is
what the rule currently means, and this is exactly the amendment T056 made real elsewhere.

**Why a batch summary reaches beyond `b:<batch_id>` (FR-024a).** A feed-timeliness finding has no
batch — that is the entire reason `subject_type: source_period` and a nullable `finding.batch_id`
exist (`src/dq/rules/library/feed_late_missing.yaml`). Summarising only the batch's own scope key
would never surface it: a batch that arrived can still belong to a period a *different*, still-
missing delivery was expected in, and nothing about the arrived batch itself looks wrong. So a batch
summary also pulls the `sp:<code>:<period>` scope key covering the same source and period.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from dq.engine.runner import BatchScope, ResolvedScope, RunScope, resolve_scope

# ---------------------------------------------------------------------------
# T068 — query_findings
# ---------------------------------------------------------------------------


class FindingRecord(BaseModel):
    """One finding, fully attributable without a separate lookup (FR-023, US1/AC4, SC-002)."""

    model_config = ConfigDict(frozen=True)

    finding_id: int
    subject_type: str
    subject_key: str
    offending_value: str | None
    observed_value: str | None
    expected_value: str | None
    severity: str
    detected_at: datetime
    rule_key: str
    rule_version_id: int
    version_no: int
    domain: str
    dimension: str
    owning_function: str


_FINDINGS_SELECT = """
    SELECT f.finding_id, f.subject_type, f.subject_key, f.offending_value,
           f.observed_value, f.expected_value, f.severity, f.detected_at,
           r.rule_key, rv.rule_version_id, rv.version_no,
           r.domain, r.dimension, r.owning_function
    FROM   finding f
    JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
    JOIN   rule r           ON r.rule_id = rv.rule_id
"""


def query_findings(
    conn: Connection,
    *,
    domain: str | None = None,
    rule_key: str | None = None,
    severity: str | None = None,
    batch_id: int | None = None,
    detected_from: datetime | None = None,
    detected_to: datetime | None = None,
) -> list[FindingRecord]:
    """Findings matching every given filter, in any combination (FR-023).

    All-None returns every finding. Filters are ANDed and independent — a batch filter and a domain
    filter narrow together, never inferring one from the other.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if domain is not None:
        clauses.append("r.domain = :domain")
        params["domain"] = domain
    if rule_key is not None:
        clauses.append("r.rule_key = :rule_key")
        params["rule_key"] = rule_key
    if severity is not None:
        clauses.append("f.severity = :severity")
        params["severity"] = severity
    if batch_id is not None:
        clauses.append("f.batch_id = :batch_id")
        params["batch_id"] = batch_id
    if detected_from is not None:
        clauses.append("f.detected_at >= :detected_from")
        params["detected_from"] = detected_from
    if detected_to is not None:
        clauses.append("f.detected_at < :detected_to")
        params["detected_to"] = detected_to

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        text(f"{_FINDINGS_SELECT} {where} ORDER BY f.detected_at, f.finding_id"),
        params,
    )
    return [FindingRecord.model_validate(dict(row._mapping)) for row in rows]


# ---------------------------------------------------------------------------
# T069 — summarise_scope / summarise_batch
# ---------------------------------------------------------------------------


class _DistinctFinding(NamedTuple):
    """One `(rule_id, subject_key)` pair, already collapsed to its newest-version row.

    Kept separate from the SQL that produces it so the arithmetic below — the part T067 tests — is
    plain Python over plain tuples, not something only provable by querying a live database.
    """

    rule_id: int
    rule_key: str
    domain: str
    subject_key: str
    severity: str


@dataclass(frozen=True, slots=True)
class RuleCount:
    rule_key: str
    domain: str
    finding_count: int


@dataclass(frozen=True, slots=True)
class ErroredRule:
    rule_key: str
    version_no: int
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class ScopeSummary:
    """Grouped counts for one evaluated scope, reconciling exactly against `finding` (FR-025)."""

    scope_keys: tuple[str, ...]
    total_findings: int
    by_domain: dict[str, int]
    by_rule: tuple[RuleCount, ...]
    by_severity: dict[str, int]
    errored_rules: tuple[ErroredRule, ...]


def _aggregate(
    rows: Sequence[_DistinctFinding],
) -> tuple[dict[str, int], tuple[RuleCount, ...], dict[str, int]]:
    """Group already-deduplicated findings by domain, by rule, and by severity.

    Pure arithmetic over `rows` — no connection, no query. All three groupings partition the same
    set, so their counts always sum to `len(rows)`; that invariant is what `test_summary_math.py`
    checks.
    """
    by_domain: dict[str, int] = {}
    by_rule: dict[tuple[int, str, str], int] = {}
    by_severity: dict[str, int] = {}

    for row in rows:
        by_domain[row.domain] = by_domain.get(row.domain, 0) + 1
        rule_key = (row.rule_id, row.rule_key, row.domain)
        by_rule[rule_key] = by_rule.get(rule_key, 0) + 1
        by_severity[row.severity] = by_severity.get(row.severity, 0) + 1

    rule_counts = tuple(
        RuleCount(rule_key=rule_key, domain=domain, finding_count=count)
        for (_, rule_key, domain), count in sorted(by_rule.items(), key=lambda kv: kv[0][1])
    )
    return by_domain, rule_counts, by_severity


#: `CAST(:scope_keys AS text[])` mirrors `dq.rules.registry._RECORDED_VERSIONS`'s
#: `CAST(:version_ids AS bigint[])` — psycopg needs the explicit array cast to bind a Python list
#: through a raw `text()` statement; `ANY(:scope_keys)` alone leaves the parameter's type
#: ambiguous and fails to bind.
_DISTINCT_FINDINGS = """
    SELECT DISTINCT ON (r.rule_id, f.subject_key)
           r.rule_id, r.rule_key, r.domain, f.subject_key, f.severity
    FROM   finding f
    JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
    JOIN   rule r           ON r.rule_id = rv.rule_id
    WHERE  f.scope_key = ANY(CAST(:scope_keys AS text[]))
    ORDER  BY r.rule_id, f.subject_key, rv.version_no DESC
"""


def _source_period_scope_key(conn: Connection, resolved: ResolvedScope) -> str | None:
    """The `sp:` scope key covering the same source and period as a batch scope (FR-024a).

    `None` for a scope that is already `source_period` — it has no separate batch to reach beyond.
    """
    if resolved.scope_type != "batch" or resolved.source_system_id is None:
        return None
    code = conn.execute(
        text("SELECT code FROM source_system WHERE source_system_id = :id"),
        {"id": resolved.source_system_id},
    ).scalar_one()
    return f"sp:{code}:{resolved.period_start:%Y-%m}"


def _latest_run_id(conn: Connection, resolved: ResolvedScope) -> int | None:
    """The most recent `rule_run` that actually evaluated this scope, or `None` if never run.

    Only the latest run's outcomes are reported: a rule that errored once and succeeded on a later
    re-run should not haunt the summary forever.
    """
    if resolved.scope_type == "batch":
        return conn.execute(
            text(
                "SELECT rule_run_id FROM rule_run "
                "WHERE scope_type = 'batch' AND batch_id = :batch_id "
                "ORDER BY rule_run_id DESC LIMIT 1"
            ),
            {"batch_id": resolved.batch_id},
        ).scalar_one_or_none()
    return conn.execute(
        text(
            "SELECT rule_run_id FROM rule_run "
            "WHERE scope_type = 'source_period' "
            "  AND scope_source_system_id = :source_id "
            "  AND scope_period = daterange(:period_start, :period_end, '[)') "
            "ORDER BY rule_run_id DESC LIMIT 1"
        ),
        {
            "source_id": resolved.source_system_id,
            "period_start": resolved.period_start,
            "period_end": resolved.period_end,
        },
    ).scalar_one_or_none()


def _errored_rules_for(conn: Connection, resolved: ResolvedScope) -> tuple[ErroredRule, ...]:
    run_id = _latest_run_id(conn, resolved)
    if run_id is None:
        return ()
    rows = conn.execute(
        text(
            "SELECT r.rule_key, rv.version_no, rrv.error_detail "
            "FROM rule_run_rule_version rrv "
            "JOIN rule_version rv ON rv.rule_version_id = rrv.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE rrv.rule_run_id = :run_id AND rrv.outcome = 'ERRORED' "
            "ORDER BY r.rule_key"
        ),
        {"run_id": run_id},
    ).all()
    return tuple(
        ErroredRule(rule_key=row.rule_key, version_no=row.version_no, error_detail=row.error_detail)
        for row in rows
    )


def summarise_scope(conn: Connection, scope: RunScope) -> ScopeSummary:
    """Group findings by domain, by rule, and by severity for one evaluated scope (FR-024)."""
    resolved = resolve_scope(conn, scope)
    scope_keys = [resolved.scope_key]
    period_scope_key = _source_period_scope_key(conn, resolved)
    if period_scope_key is not None:
        scope_keys.append(period_scope_key)

    raw_rows = conn.execute(text(_DISTINCT_FINDINGS), {"scope_keys": scope_keys}).all()
    rows = [
        _DistinctFinding(
            rule_id=row.rule_id,
            rule_key=row.rule_key,
            domain=row.domain,
            subject_key=row.subject_key,
            severity=row.severity,
        )
        for row in raw_rows
    ]
    by_domain, rule_counts, by_severity = _aggregate(rows)

    return ScopeSummary(
        scope_keys=tuple(scope_keys),
        total_findings=len(rows),
        by_domain=by_domain,
        by_rule=rule_counts,
        by_severity=by_severity,
        errored_rules=_errored_rules_for(conn, resolved),
    )


def summarise_batch(conn: Connection, batch_id: int) -> ScopeSummary:
    """Convenience wrapper over `summarise_scope` for the common case of a known batch id."""
    return summarise_scope(conn, BatchScope(batch_id))
