"""Rule registration and lookup (T036).

Registering a rule is the whole of SC-007: a new rule family must produce findings on the next run
with no change to engine code. Nothing here knows any rule's name, and the runner reads only
``rule_version`` rows — so adding a rule is inserting a row, not editing a module.

Connects as **``dq_author``**, which holds INSERT on ``rule`` and ``rule_version`` and has no access
to commercial data at all. That separation is the reason the determinism check has to live in the
database: this role can reach ``rule_version`` directly, so a check in this module is a courtesy,
not a control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from dq.rules.definition import RuleDefinition


@dataclass(frozen=True, slots=True)
class RegisteredVersion:
    """What registration did, so a caller can report it without querying again."""

    rule_key: str
    rule_id: int
    rule_version_id: int
    version_no: int
    #: ``created`` — new rule; ``versioned`` — semantic change appended a version; ``unchanged``.
    outcome: str


@dataclass(frozen=True, slots=True)
class ActiveRuleVersion:
    """One rule version the engine will evaluate."""

    rule_id: int
    rule_key: str
    rule_version_id: int
    version_no: int
    domain: str
    dimension: str
    severity: str
    subject_type: str
    predicate_sql: str
    parameters: dict[str, Any]


_LATEST_VERSION = text("""
SELECT rv.rule_version_id, rv.version_no, rv.severity, rv.subject_type,
       rv.predicate_sql, rv.parameters
FROM   rule_version rv
WHERE  rv.rule_id = :rule_id
ORDER  BY rv.version_no DESC
LIMIT  1
""")

#: The engine evaluates the newest version of every **active** rule.
#:
#: Newest rather than all: a rule with three versions describes one check that changed twice, and
#: evaluating all three would raise three findings for one problem. Deactivating stops new findings
#: without touching historical ones (FR-007, SC-006), which is why the filter is on `rule.is_active`
#: and never on the version.
#:
#: Two SQLAlchemy `text()` hazards are avoided below, both of which produce confusing errors:
#:
#: * ``CAST(:rule_keys AS text[])`` rather than the postfix ``::text[]``. SQLAlchemy declines to
#:   treat ``:name`` as a bind parameter when a colon follows it — that is how it avoids eating cast
#:   operators — so the postfix form leaves a literal ``:rule_keys`` for PostgreSQL to reject.
#: * No colon-prefixed word appears in a SQL comment anywhere in this statement. ``text()`` scans
#:   comments too, so a ``:name`` written in prose becomes a required bind parameter.
_ACTIVE_VERSIONS = text("""
SELECT r.rule_id, r.rule_key, r.domain, r.dimension,
       rv.rule_version_id, rv.version_no, rv.severity, rv.subject_type,
       rv.predicate_sql, rv.parameters
FROM   rule r
JOIN   LATERAL (
         SELECT rv2.*
         FROM   rule_version rv2
         WHERE  rv2.rule_id = r.rule_id
         ORDER  BY rv2.version_no DESC
         LIMIT  1
       ) rv ON true
WHERE  r.is_active
  AND  rv.subject_type = ANY(:subject_types)
  AND  (CAST(:rule_keys AS text[]) IS NULL OR r.rule_key = ANY(:rule_keys))
ORDER  BY r.rule_key
""")


def register(conn: Connection, definition: RuleDefinition) -> RegisteredVersion:
    """Create or version a rule from ``definition``.

    Outcomes follow contracts/rule-definition.md:

    ==========================================  ===================
    Condition                                   Result
    ==========================================  ===================
    ``rule_key`` unseen                         rule created, version 1
    exists, a versioned field changed           new version appended
    exists, nothing semantic changed            no-op
    ==========================================  ===================

    ``owning_function`` and ``is_active`` are updated in place when they differ, because neither
    changes what a finding means — versioning on them would make the history unreadable for no gain.
    """
    row = conn.execute(
        text("SELECT rule_id, owning_function FROM rule WHERE rule_key = :k"),
        {"k": definition.rule_key},
    ).one_or_none()

    if row is None:
        rule_id = conn.execute(
            text(
                "INSERT INTO rule (rule_key, domain, dimension, owning_function) "
                "VALUES (:k, :d, :dim, :own) RETURNING rule_id"
            ),
            {
                "k": definition.rule_key,
                "d": definition.domain,
                "dim": definition.dimension,
                "own": definition.owning_function,
            },
        ).scalar_one()
        version_id = _insert_version(conn, rule_id, 1, definition)
        return RegisteredVersion(definition.rule_key, rule_id, version_id, 1, "created")

    rule_id = row.rule_id
    if row.owning_function != definition.owning_function:
        conn.execute(
            text("UPDATE rule SET owning_function = :own WHERE rule_id = :id"),
            {"own": definition.owning_function, "id": rule_id},
        )

    latest = conn.execute(_LATEST_VERSION, {"rule_id": rule_id}).one()
    if not definition.differs_semantically_from(
        latest.severity, latest.subject_type, latest.predicate_sql, latest.parameters
    ):
        return RegisteredVersion(
            definition.rule_key, rule_id, latest.rule_version_id, latest.version_no, "unchanged"
        )

    next_no = latest.version_no + 1
    version_id = _insert_version(conn, rule_id, next_no, definition)
    return RegisteredVersion(definition.rule_key, rule_id, version_id, next_no, "versioned")


def _insert_version(
    conn: Connection, rule_id: int, version_no: int, definition: RuleDefinition
) -> int:
    """Insert one ``rule_version``. The T025 trigger validates the predicate as this runs."""
    return int(
        conn.execute(
            text(
                "INSERT INTO rule_version "
                "(rule_id, version_no, severity, subject_type, predicate_sql, parameters) "
                "VALUES (:rid, :no, :sev, :st, :sql, CAST(:params AS jsonb)) "
                "RETURNING rule_version_id"
            ),
            {
                "rid": rule_id,
                "no": version_no,
                "sev": definition.severity,
                "st": definition.subject_type,
                "sql": definition.predicate_sql,
                "params": _json(definition.parameters),
            },
        ).scalar_one()
    )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def active_versions(
    conn: Connection, subject_types: list[str], rule_keys: list[str] | None = None
) -> list[ActiveRuleVersion]:
    """The rule versions the engine should evaluate for a scope."""
    rows = conn.execute(
        _ACTIVE_VERSIONS, {"subject_types": subject_types, "rule_keys": rule_keys}
    ).all()
    return [
        ActiveRuleVersion(
            rule_id=r.rule_id,
            rule_key=r.rule_key,
            rule_version_id=r.rule_version_id,
            version_no=r.version_no,
            domain=r.domain,
            dimension=r.dimension,
            severity=r.severity,
            subject_type=r.subject_type,
            predicate_sql=r.predicate_sql,
            parameters=r.parameters or {},
        )
        for r in rows
    ]


def retire_feed_expectation(conn: Connection, source_system_id: int, end_date: date) -> bool:
    """End-date a feed expectation so later periods stop producing missing-feed findings (FR-021c).

    Retirement is an end-date, not a delete. Findings already raised against this expectation stay
    explicable — the declaration that produced them is still readable, with a range showing exactly
    when it stopped applying. Deleting the row would leave those findings citing a rule about a feed
    nobody can look up.

    The range keeps its original lower bound; only the upper bound moves. ``dq_author`` holds
    ``UPDATE (active)`` and nothing more (migration 0009), so the cadence and delivery window that
    historical findings were judged against cannot be rewritten underneath them.

    Returns False if no expectation exists for that source.
    """
    result = conn.execute(
        text(
            "UPDATE feed_expectation "
            "SET active = daterange(lower(active), :end_date, '[)') "
            "WHERE source_system_id = :sid"
        ),
        {"end_date": end_date, "sid": source_system_id},
    )
    return result.rowcount > 0


def set_active(conn: Connection, rule_key: str, active: bool) -> bool:
    """Activate or deactivate a rule. Returns False if the key is unknown.

    Touches ``rule.is_active`` only. Historical findings keep their meaning; the rule simply stops
    producing new ones (FR-007).
    """
    result = conn.execute(
        text("UPDATE rule SET is_active = :a WHERE rule_key = :k"),
        {"a": active, "k": rule_key},
    )
    return result.rowcount > 0
