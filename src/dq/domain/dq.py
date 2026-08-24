"""The ``dq`` schema — rule registry, runs, findings, feed expectations.

``rule_version`` and ``finding`` are immutable by trigger (T024). They are the audit-bearing
tables: a finding says what was true at a moment, and a rule version says what "failed" meant at
that moment. Either one being editable would make every historical verdict unprovable, which is
the reconstructability half of constitution principle V.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Interval,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import DATERANGE, JSONB, UUID

from dq.db.schemas import Schema
from dq.domain import metadata

_S: Final[str] = Schema.DQ.value
_C: Final[str] = Schema.COMMERCIAL.value

#: Enumerated domains. `CHECK`-constrained rather than a PostgreSQL enum type, because adding a
#: value to an enum type is a migration while adding one here is a one-line constraint change —
#: and SC-007 requires new rules without engine changes.
DOMAINS: Final[tuple[str, ...]] = ("SALES", "HCP", "HCO", "PRODUCT", "TERRITORY_ALIGNMENT")

#: The seven quality dimensions named in the spec.
DIMENSIONS: Final[tuple[str, ...]] = (
    "completeness",
    "uniqueness",
    "validity",
    "referential_integrity",
    "timeliness",
    "consistency",
    "conformity",
)

SEVERITIES: Final[tuple[str, ...]] = ("HIGH", "MEDIUM", "LOW")

#: What a finding points at. `record` names a row; `batch` and `source_period` are aggregate
#: subjects that may have no row at all — a feed that never arrived being the motivating case.
SUBJECT_TYPES: Final[tuple[str, ...]] = ("record", "batch", "source_period")

#: `COMPLETED_WITH_ERRORS` is distinct from `COMPLETED` because revision 1 closed a run as
#: `COMPLETED` when a rule errored, making a partially-evaluated batch indistinguishable from a
#: clean one in the steward's headline view — the more dangerous of the two failure modes.
RUN_STATUSES: Final[tuple[str, ...]] = (
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
)

RULE_OUTCOMES: Final[tuple[str, ...]] = ("EVALUATED", "ERRORED")

CADENCES: Final[tuple[str, ...]] = ("DAILY", "WEEKLY", "MONTHLY")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    rendered = ",".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


# ---------------------------------------------------------------------------
# T020 — rule, rule_version
# ---------------------------------------------------------------------------

rule = Table(
    "rule",
    metadata,
    Column("rule_id", Integer, Identity(always=False), primary_key=True),
    # Stable and referenced by findings forever. Renaming one orphans history.
    Column("rule_key", Text, nullable=False, unique=True),
    # All four declarations NOT NULL — FR-005 enforced by the schema, not by validation code that
    # can be forgotten or bypassed.
    Column("domain", Text, nullable=False),
    Column("dimension", Text, nullable=False),
    Column("owning_function", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    CheckConstraint(_in_list("domain", DOMAINS), name="domain_enum"),
    CheckConstraint(_in_list("dimension", DIMENSIONS), name="dimension_enum"),
    schema=_S,
)


rule_version = Table(
    "rule_version",
    metadata,
    Column("rule_version_id", BigInteger, Identity(always=False), primary_key=True),
    Column("rule_id", Integer, ForeignKey(f"{_S}.rule.rule_id"), nullable=False),
    Column("version_no", Integer, nullable=False),
    # Versioned, because a severity change alters what a finding means to a steward.
    Column("severity", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("predicate_sql", Text, nullable=False),
    Column("parameters", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in_list("severity", SEVERITIES), name="severity_enum"),
    CheckConstraint(_in_list("subject_type", SUBJECT_TYPES), name="subject_type_enum"),
    UniqueConstraint("rule_id", "version_no"),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T021 — feed_expectation
# ---------------------------------------------------------------------------

feed_expectation = Table(
    "feed_expectation",
    metadata,
    Column("feed_expectation_id", Integer, Identity(always=False), primary_key=True),
    Column(
        "source_system_id",
        SmallInteger,
        ForeignKey(f"{_C}.source_system.source_system_id"),
        nullable=False,
    ),
    Column("cadence", Text, nullable=False),
    # Grace period after period end before the feed counts as late.
    Column("delivery_window", Interval, nullable=False),
    # End-dated to retire an expectation (FR-021c): periods after the end date stop producing
    # missing-feed findings, while findings raised before it are retained.
    Column("active", DATERANGE, nullable=False),
    CheckConstraint(_in_list("cadence", CADENCES), name="cadence_enum"),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T022 — rule_run, rule_run_rule_version
# ---------------------------------------------------------------------------

rule_run = Table(
    "rule_run",
    metadata,
    Column("rule_run_id", BigInteger, Identity(always=False), primary_key=True),
    # Constitution principle V. Features 2+ join their audit rows on this.
    Column("correlation_id", UUID(as_uuid=True), nullable=False),
    Column("scope_type", Text, nullable=False),
    Column("batch_id", BigInteger, ForeignKey(f"{_C}.data_batch.batch_id"), nullable=True),
    Column(
        "scope_source_system_id",
        SmallInteger,
        ForeignKey(f"{_C}.source_system.source_system_id"),
        nullable=True,
    ),
    Column("scope_period", DATERANGE, nullable=True),
    # The three columns that make reproducibility checkable rather than asserted. `as_of_date`
    # pins the business world, `reference_watermark` the delivered world, `session_settings` the
    # execution world. Two runs sharing all three over immutable data must produce identical
    # findings (research.md D3).
    Column("as_of_date", Date, nullable=False),
    Column("reference_watermark", BigInteger, nullable=False),
    Column("session_settings", JSONB, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("error_detail", Text, nullable=True),
    CheckConstraint(_in_list("status", RUN_STATUSES), name="status_enum"),
    CheckConstraint("scope_type IN ('batch','source_period')", name="scope_type_enum"),
    # A batch scope carries a batch; a source-period scope carries a source and a period. Without
    # this, a run could record a scope it did not actually evaluate.
    CheckConstraint(
        "(scope_type = 'batch') = (batch_id IS NOT NULL)",
        name="batch_scope_has_batch",
    ),
    CheckConstraint(
        "(scope_type = 'source_period') "
        "= (scope_source_system_id IS NOT NULL AND scope_period IS NOT NULL)",
        name="period_scope_has_source_and_period",
    ),
    schema=_S,
)


rule_run_rule_version = Table(
    "rule_run_rule_version",
    metadata,
    Column("rule_run_id", BigInteger, ForeignKey(f"{_S}.rule_run.rule_run_id"), nullable=False),
    Column(
        "rule_version_id",
        BigInteger,
        ForeignKey(f"{_S}.rule_version.rule_version_id"),
        nullable=False,
    ),
    # FR-014. Recorded per rule, not per record: set-based execution cannot do otherwise, since
    # one malformed value aborts the whole statement (research.md R3).
    Column("outcome", Text, nullable=False),
    Column("finding_count", Integer, nullable=False, server_default=text("0")),
    Column("error_detail", Text, nullable=True),
    PrimaryKeyConstraint("rule_run_id", "rule_version_id"),
    CheckConstraint(_in_list("outcome", RULE_OUTCOMES), name="outcome_enum"),
    CheckConstraint("finding_count >= 0", name="finding_count_non_negative"),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T023 — finding
# ---------------------------------------------------------------------------

finding = Table(
    "finding",
    metadata,
    Column("finding_id", BigInteger, Identity(always=False), primary_key=True),
    Column("rule_run_id", BigInteger, ForeignKey(f"{_S}.rule_run.rule_run_id"), nullable=False),
    Column(
        "rule_version_id",
        BigInteger,
        ForeignKey(f"{_S}.rule_version.rule_version_id"),
        nullable=False,
    ),
    # NULLABLE. A feed that never arrived has no batch by definition, and revision 1's NOT NULL
    # made that finding unrecordable (research.md D4).
    Column("batch_id", BigInteger, ForeignKey(f"{_C}.data_batch.batch_id"), nullable=True),
    # `b:<batch_id>` or `sp:<source_code>:<period>`. Never null, for every subject type — which is
    # what lets one uniqueness key serve all three and keeps idempotency uniform.
    Column("scope_key", Text, nullable=False),
    Column("subject_type", Text, nullable=False),
    Column("subject_key", Text, nullable=False),
    Column("offending_value", Text, nullable=True),
    Column("observed_value", Text, nullable=True),
    Column("expected_value", Text, nullable=True),
    # Snapshotted from the rule version, so a later severity change cannot rewrite history.
    Column("severity", Text, nullable=False),
    Column("detected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Idempotency as a database constraint rather than an application check (FR-011, SC-003).
    # Keyed on `scope_key`, not `batch_id`: revision 1's key would have re-fired the same missing
    # period as a fresh finding against every subsequent batch.
    UniqueConstraint("rule_version_id", "scope_key", "subject_key"),
    CheckConstraint(_in_list("subject_type", SUBJECT_TYPES), name="subject_type_enum"),
    CheckConstraint(_in_list("severity", SEVERITIES), name="severity_enum"),
    # A record subject has a batch; an aggregate subject does not.
    CheckConstraint(
        "(subject_type = 'record') = (batch_id IS NOT NULL)",
        name="record_subject_has_batch",
    ),
    # SC-002's mechanism: every record-subject finding carries its offending value or the insert
    # fails. Without this the criterion was an aspiration with nothing behind it.
    CheckConstraint(
        "subject_type <> 'record' OR offending_value IS NOT NULL",
        name="record_subject_has_offending_value",
    ),
    schema=_S,
)


_indexes = [
    Index("ix_finding_scope_severity", finding.c.scope_key, finding.c.severity),
    Index("ix_finding_rule_version", finding.c.rule_version_id),
    Index("ix_finding_detected_at", finding.c.detected_at),
    Index("ix_finding_subject", finding.c.subject_type, finding.c.subject_key),
    Index("ix_rule_run_correlation", rule_run.c.correlation_id),
]

#: Index names T018's test asserts exist.
EXPECTED_INDEXES: Final[tuple[str, ...]] = tuple(ix.name for ix in _indexes if ix.name)

#: Tables that reject UPDATE and DELETE outright (T024).
IMMUTABLE_TABLES: Final[tuple[str, ...]] = ("rule_version", "finding")
