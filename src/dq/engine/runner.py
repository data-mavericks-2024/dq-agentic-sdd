"""The rule runner (T037, T038, T039).

Executes every active rule version against a **scope** and persists one finding per failing subject.

Three things about this module are load-bearing, and each fixes a specific way the first design was
wrong:

**A run takes a scope, not a batch.** ``run_rules(batch_id)`` could not express "check whether the
September feed from Veeva ever arrived" — the case that has no batch by definition, and the one
FR-021d and SC-009 require.

**One statement per rule, not per record.** Nine rules over a million transactions is nine
statements. Row-by-row evaluation across the link to Singapore misses SC-008's ten-minute budget by
orders of magnitude, and no amount of tuning recovers that.

**Three values pin the world the run saw.** ``as_of_date`` pins the business world — which master
version applies. ``reference_watermark`` pins the delivered world — which batches had arrived.
``session_settings`` pins the execution world. All three are recorded on ``rule_run``, so two runs
can be *compared* rather than assumed equal. Idempotency (the unique constraint) guarantees
non-duplication; only these three guarantee identity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.rules.predicates import bound_parameter_names
from dq.rules.registry import ActiveRuleVersion, active_versions, recorded_versions


class ScopeNotFoundError(LookupError):
    """The scope resolves to neither a batch nor a declared feed expectation."""


class InvalidRunRequestError(ValueError):
    """Fresh and replay execution modes were combined or left unspecified."""


class ReplaySourceError(ValueError):
    """The requested source run is absent or is not an original completed run."""


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchScope:
    """One physical arrival."""

    batch_id: int


@dataclass(frozen=True, slots=True)
class SourcePeriodScope:
    """A source system and a business period, whether or not anything arrived for it."""

    source_system_code: str
    period_start: date
    period_end: date  # exclusive


RunScope = BatchScope | SourcePeriodScope


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """A scope with the reference world pinned.

    The period is carried as two plain dates rather than a range object. Range types travel badly
    through a raw ``text()`` statement — SQLAlchemy's ``Range`` has no psycopg adapter, and
    psycopg's own ``Range`` has no ``bounds`` attribute to read the convention back off. Two dates
    plus the half-open ``[)`` convention stated in SQL at each call site has neither problem, and
    makes the convention visible where it matters.
    """

    scope_type: Literal["batch", "source_period"]
    scope_key: str
    as_of_date: date
    reference_watermark: int
    batch_id: int | None
    source_system_id: int | None
    #: Inclusive lower bound.
    period_start: date | None
    #: **Exclusive** upper bound.
    period_end: date | None

    @property
    def subject_types(self) -> list[str]:
        """Which rules apply here.

        A batch scope evaluates record-level and batch-level rules. A source-period scope evaluates
        only aggregate rules, because it may have no rows at all to point at.
        """
        return ["record", "batch"] if self.scope_type == "batch" else ["source_period"]


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_key: str
    rule_version_id: int
    version_no: int
    outcome: Literal["EVALUATED", "ERRORED"]
    finding_count: int
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuleRunResult:
    rule_run_id: int
    correlation_id: uuid.UUID
    scope_key: str
    as_of_date: date
    reference_watermark: int
    status: str
    outcomes: list[RuleOutcome] = field(default_factory=list)

    @property
    def finding_count(self) -> int:
        return sum(o.finding_count for o in self.outcomes)

    @property
    def errored(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.outcome == "ERRORED"]


# ---------------------------------------------------------------------------
# Scope resolution (T037)
# ---------------------------------------------------------------------------


def _business_end(period_end: date | None) -> date:
    """The last date *inside* a half-open ``[from, to)`` period.

    Taking the exclusive upper bound directly would admit a master version effective the day after
    the period ended — outside the world the run is supposed to see.
    """
    if period_end is None:
        raise ValueError(
            "business_period has no upper bound; an unbounded scope has no as-of date."
        )
    return period_end - timedelta(days=1)


def _watermark(conn: Connection) -> int:
    """The highest ``batch_id`` visible right now.

    Zero when nothing has been delivered, so an empty database still produces a runnable scope
    rather than a null that propagates into every predicate.
    """
    return int(conn.execute(text("SELECT coalesce(max(batch_id), 0) FROM data_batch")).scalar_one())


@dataclass(frozen=True, slots=True)
class ReplayExecution:
    """The complete immutable execution context selected by an original run."""

    resolved: ResolvedScope
    session_settings: dict[str, str]
    versions: list[ActiveRuleVersion]


def _load_replay_execution(conn: Connection, rule_run_id: int) -> ReplayExecution:
    row = conn.execute(
        text("""
            SELECT rr.status, rr.replay_of_rule_run_id, rr.scope_type, rr.batch_id,
                   rr.scope_source_system_id,
                   lower(rr.scope_period) AS scope_period_start,
                   upper(rr.scope_period) AS scope_period_end,
                   rr.as_of_date, rr.reference_watermark, rr.session_settings,
                   b.source_system_id AS batch_source_system_id,
                   lower(b.business_period) AS batch_period_start,
                   upper(b.business_period) AS batch_period_end,
                   source.code AS source_system_code
            FROM rule_run rr
            LEFT JOIN data_batch b ON b.batch_id = rr.batch_id
            LEFT JOIN source_system source
                   ON source.source_system_id = rr.scope_source_system_id
            WHERE rr.rule_run_id = :id
        """),
        {"id": rule_run_id},
    ).one_or_none()
    if row is None:
        raise ReplaySourceError(f"No rule_run with rule_run_id={rule_run_id}.")
    if row.replay_of_rule_run_id is not None:
        raise ReplaySourceError(
            f"rule_run_id={rule_run_id} is itself a replay; replay the original run instead."
        )
    if row.status != "COMPLETED":
        raise ReplaySourceError(
            f"rule_run_id={rule_run_id} has status {row.status}; only COMPLETED is replayable."
        )

    raw_settings = row.session_settings
    if not isinstance(raw_settings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_settings.items()
    ):
        raise ReplaySourceError(
            f"rule_run_id={rule_run_id} has invalid persisted session_settings."
        )
    session_settings = dict(raw_settings)

    if row.scope_type == "batch":
        if (
            row.batch_id is None
            or row.batch_source_system_id is None
            or row.batch_period_start is None
            or row.batch_period_end is None
        ):
            raise ReplaySourceError(f"rule_run_id={rule_run_id} has an invalid batch scope.")
        resolved = ResolvedScope(
            scope_type="batch",
            scope_key=f"b:{row.batch_id}",
            as_of_date=row.as_of_date,
            reference_watermark=row.reference_watermark,
            batch_id=row.batch_id,
            source_system_id=row.batch_source_system_id,
            period_start=row.batch_period_start,
            period_end=row.batch_period_end,
        )
    elif row.scope_type == "source_period":
        if (
            row.scope_source_system_id is None
            or row.scope_period_start is None
            or row.scope_period_end is None
            or row.source_system_code is None
        ):
            raise ReplaySourceError(
                f"rule_run_id={rule_run_id} has an invalid source-period scope."
            )
        resolved = ResolvedScope(
            scope_type="source_period",
            scope_key=f"sp:{row.source_system_code}:{row.scope_period_start:%Y-%m}",
            as_of_date=row.as_of_date,
            reference_watermark=row.reference_watermark,
            batch_id=None,
            source_system_id=row.scope_source_system_id,
            period_start=row.scope_period_start,
            period_end=row.scope_period_end,
        )
    else:
        raise ReplaySourceError(
            f"rule_run_id={rule_run_id} has unsupported scope_type={row.scope_type!r}."
        )

    version_ids = [
        int(value)
        for value in conn.execute(
            text(
                "SELECT rule_version_id FROM rule_run_rule_version "
                "WHERE rule_run_id = :id ORDER BY rule_version_id"
            ),
            {"id": rule_run_id},
        ).scalars()
    ]
    try:
        versions = recorded_versions(conn, version_ids)
    except LookupError as exc:
        raise ReplaySourceError(f"rule_run_id={rule_run_id}: {exc}") from exc
    if any(version.subject_type not in resolved.subject_types for version in versions):
        raise ReplaySourceError(
            f"rule_run_id={rule_run_id} records a rule version incompatible with its scope."
        )
    return ReplayExecution(resolved, session_settings, versions)


def resolve_scope(conn: Connection, scope: RunScope) -> ResolvedScope:
    """Bind a fresh evaluation scope to its current as-of date and watermark."""
    watermark = _watermark(conn)

    if isinstance(scope, BatchScope):
        # `lower`/`upper` rather than the range itself: a range object crossing the Python boundary
        # through a raw text() statement has no working adapter in either direction.
        row = conn.execute(
            text(
                "SELECT b.batch_id, b.source_system_id, "
                "       lower(b.business_period) AS period_start, "
                "       upper(b.business_period) AS period_end "
                "FROM data_batch b WHERE b.batch_id = :id"
            ),
            {"id": scope.batch_id},
        ).one_or_none()
        if row is None:
            raise ScopeNotFoundError(f"No data_batch with batch_id={scope.batch_id}.")
        return ResolvedScope(
            scope_type="batch",
            scope_key=f"b:{row.batch_id}",
            as_of_date=_business_end(row.period_end),
            reference_watermark=watermark,
            batch_id=row.batch_id,
            source_system_id=row.source_system_id,
            period_start=row.period_start,
            period_end=row.period_end,
        )

    source_id = conn.execute(
        text("SELECT source_system_id FROM source_system WHERE code = :c"),
        {"c": scope.source_system_code},
    ).scalar_one_or_none()
    if source_id is None:
        raise ScopeNotFoundError(f"No source_system with code={scope.source_system_code!r}.")

    return ResolvedScope(
        scope_type="source_period",
        scope_key=f"sp:{scope.source_system_code}:{scope.period_start:%Y-%m}",
        as_of_date=_business_end(scope.period_end),
        reference_watermark=watermark,
        batch_id=None,
        source_system_id=source_id,
        period_start=scope.period_start,
        period_end=scope.period_end,
    )


# ---------------------------------------------------------------------------
# Execution (T038) and error capture (T039)
# ---------------------------------------------------------------------------

#: The predicate is embedded, not parameterised, because DDL-position SQL cannot be bound — but it
#: is a *stored, trigger-validated* predicate, never a string from a caller or a model. Every value
#: inside it is bound.
_INSERT_FINDINGS = """
INSERT INTO finding (rule_run_id, rule_version_id, batch_id, scope_key, subject_type,
                     subject_key, offending_value, observed_value, expected_value,
                     severity, detected_at)
SELECT :run_id, :rule_version_id, :finding_batch_id, :scope_key, :subject_type,
       p.subject_key, p.offending_value, p.observed_value, p.expected_value,
       :severity, now()
FROM ( {predicate} ) AS p
ON CONFLICT (rule_version_id, scope_key, subject_key) DO NOTHING
"""


def _engine_bindings(resolved: ResolvedScope) -> dict[str, Any]:
    """Everything the engine supplies, whatever the rule.

    The three ``scope_*`` values are what let an aggregate rule answer "did the September feed from
    Veeva arrive?" rather than "did any declared feed ever miss a period?" — see migration 0006.
    """
    return {
        "batch_id": resolved.batch_id,
        "as_of_date": resolved.as_of_date,
        "reference_watermark": resolved.reference_watermark,
        "scope_source_system_id": resolved.source_system_id,
        "scope_period_start": resolved.period_start,
        "scope_period_end": resolved.period_end,
    }


def _bindings_for(version: ActiveRuleVersion, resolved: ResolvedScope) -> dict[str, Any]:
    """Bind only what the predicate declares.

    Handing SQLAlchemy a parameter the statement does not mention is an error, and every rule
    declares a different subset — an aggregate rule over ``feed_expectation`` has no ``:batch_id``.
    """
    # Engine bindings win. A rule that declared `batch_id` in its own `parameters` would otherwise
    # pin itself to one batch forever and quietly stop evaluating the scope it was handed.
    available = {**version.parameters, **_engine_bindings(resolved)}
    needed = bound_parameter_names(version.predicate_sql)
    missing = needed - available.keys()
    if missing:
        raise KeyError(
            f"predicate binds {sorted(missing)} which is neither engine-bound nor declared in "
            f"`parameters`. Registration should have rejected this (rule 8)."
        )
    return {name: available[name] for name in needed}


def _evaluate_one(
    conn: Connection, run_id: int, version: ActiveRuleVersion, resolved: ResolvedScope
) -> RuleOutcome:
    """Execute one rule version. Never raises — a failing rule does not fail the run."""
    # Held for the rest of the transaction. Without it, two concurrent runs over one scope race for
    # which rule_run_id is stamped on each finding, making "which run produced this?" ambiguous in
    # the audit trail — the one place that must never be.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
        {"k": f"{resolved.scope_key}|{version.rule_version_id}"},
    )

    params: dict[str, Any] = {
        "run_id": run_id,
        "rule_version_id": version.rule_version_id,
        # A record subject carries its batch; an aggregate subject does not, and the CHECK
        # constraint on `finding` enforces exactly that correspondence.
        "finding_batch_id": resolved.batch_id if version.subject_type == "record" else None,
        "scope_key": resolved.scope_key,
        "subject_type": version.subject_type,
        "severity": version.severity,
    }

    savepoint = conn.begin_nested()
    try:
        params.update(_bindings_for(version, resolved))
        result = conn.execute(
            text(_INSERT_FINDINGS.format(predicate=version.predicate_sql)), params
        )
        count = result.rowcount if result.rowcount and result.rowcount > 0 else 0
        savepoint.commit()
        return RuleOutcome(
            version.rule_key, version.rule_version_id, version.version_no, "EVALUATED", count
        )
    except Exception as exc:  # deliberately broad; see below
        savepoint.rollback()
        # Broad on purpose. Any failure of one rule must leave the other rules runnable, and the
        # failure modes are open-ended: a type error in a predicate, a statement timeout, a
        # division by zero on real data. The detail is recorded rather than swallowed.
        #
        # Granularity is per rule, not per record. Set-based execution cannot do otherwise: one
        # malformed value aborts the statement and the rule contributes zero findings for the whole
        # scope — including records that would legitimately have failed (research.md R3).
        return RuleOutcome(
            version.rule_key,
            version.rule_version_id,
            version.version_no,
            "ERRORED",
            0,
            f"{type(exc).__name__}: {exc}",
        )


def _open_run(
    conn: Connection,
    resolved: ResolvedScope,
    session_settings: dict[str, str],
    replay_of: int | None,
) -> tuple[int, uuid.UUID]:
    correlation_id = uuid.uuid4()
    import json

    is_period_scope = resolved.scope_type == "source_period"
    run_id = conn.execute(
        # The range is constructed in SQL from two date parameters. `daterange(NULL, NULL, '[)')`
        # is itself NULL, so a batch scope needs no branch here — the CHECK constraint on rule_run
        # then verifies that a batch scope carries no period and a period scope does.
        text("""
            INSERT INTO rule_run (correlation_id, replay_of_rule_run_id, scope_type, batch_id,
                                  scope_source_system_id, scope_period, as_of_date,
                                  reference_watermark, session_settings, status)
            VALUES (:corr, :replay_of, :st, :bid, :ssid,
                    daterange(:period_start, :period_end, '[)'),
                    :as_of, :wm, CAST(:settings AS jsonb), 'RUNNING')
            RETURNING rule_run_id
        """),
        {
            "corr": correlation_id,
            "replay_of": replay_of,
            "st": resolved.scope_type,
            "bid": resolved.batch_id,
            "ssid": resolved.source_system_id if is_period_scope else None,
            "period_start": resolved.period_start if is_period_scope else None,
            "period_end": resolved.period_end if is_period_scope else None,
            "as_of": resolved.as_of_date,
            "wm": resolved.reference_watermark,
            "settings": json.dumps(session_settings, sort_keys=True),
        },
    ).scalar_one()
    return int(run_id), correlation_id


def _close_run(conn: Connection, run_id: int, status: str, detail: str | None = None) -> None:
    conn.execute(
        text(
            "UPDATE rule_run SET status = :s, finished_at = now(), error_detail = :d "
            "WHERE rule_run_id = :id"
        ),
        {"s": status, "d": detail, "id": run_id},
    )


def run_rules(
    settings: Settings,
    scope: RunScope | None = None,
    *,
    rule_keys: list[str] | None = None,
    replay_of: int | None = None,
) -> RuleRunResult:
    """Evaluate every active rule version against ``scope``.

    The run is opened, evaluated, and closed in **separate transactions** so that a catastrophic
    failure still leaves a ``rule_run`` row explaining itself. A run recorded only on success is a
    run that cannot report its own failure.

    Fresh evaluation requires ``scope`` and selects current active versions. ``replay_of`` is a
    distinct mode: it accepts no scope or rule filter and reuses the completed original run's scope,
    business and delivery boundaries, complete session settings, and exact recorded rule versions.
    """
    if scope is None and replay_of is None:
        raise InvalidRunRequestError("provide a scope for fresh evaluation or replay_of for replay")
    if scope is not None and replay_of is not None:
        raise InvalidRunRequestError("scope and replay_of are mutually exclusive")
    if replay_of is not None and rule_keys:
        raise InvalidRunRequestError("replay_of cannot be combined with rule_keys")

    replay: ReplayExecution | None = None
    session_settings = db.session_settings_for(settings.schema_prefix)

    with db.connection(settings, Role.ENGINE) as conn:
        # `pin_session` is called inside *every* transaction below, not once per connection. The
        # settings are applied with `set_config(..., is_local => true)`, which is transaction-scoped
        # by design — that is what stops one run's pinned environment leaking into the next. The
        # cost is that a transaction without the call has no `search_path`, so every unqualified
        # table name in it fails to resolve.
        with conn.begin():
            db.pin_session(conn, settings.schema_prefix)
            if replay_of is not None:
                replay = _load_replay_execution(conn, replay_of)
                resolved = replay.resolved
                session_settings = replay.session_settings
                try:
                    db.pin_recorded_session(conn, session_settings)
                except ValueError as exc:
                    raise ReplaySourceError(
                        f"rule_run_id={replay_of} has invalid persisted session_settings: {exc}"
                    ) from exc
            else:
                if scope is None:
                    raise InvalidRunRequestError("fresh evaluation requires a scope")
                resolved = resolve_scope(conn, scope)

        with conn.begin():
            db.pin_recorded_session(conn, session_settings)
            run_id, correlation_id = _open_run(conn, resolved, session_settings, replay_of)

        outcomes: list[RuleOutcome] = []
        try:
            with conn.begin():
                db.pin_recorded_session(conn, session_settings)
                versions = (
                    replay.versions
                    if replay is not None
                    else active_versions(conn, resolved.subject_types, rule_keys)
                )
                for version in versions:
                    outcome = _evaluate_one(conn, run_id, version, resolved)
                    outcomes.append(outcome)
                    conn.execute(
                        text(
                            "INSERT INTO rule_run_rule_version "
                            "(rule_run_id, rule_version_id, outcome, finding_count, error_detail) "
                            "VALUES (:run, :ver, :out, :n, :err)"
                        ),
                        {
                            "run": run_id,
                            "ver": outcome.rule_version_id,
                            "out": outcome.outcome,
                            "n": outcome.finding_count,
                            "err": outcome.error_detail,
                        },
                    )
        except Exception as exc:
            with conn.begin():
                db.pin_recorded_session(conn, session_settings)
                _close_run(conn, run_id, "FAILED", f"{type(exc).__name__}: {exc}")
            raise

        # A partially-evaluated scope must not read as a clean one in the steward's headline view.
        # That distinction is the whole reason COMPLETED_WITH_ERRORS is a separate status.
        errored = [o for o in outcomes if o.outcome == "ERRORED"]
        status = "COMPLETED_WITH_ERRORS" if errored else "COMPLETED"
        detail = (
            f"{len(errored)} rule(s) errored: {', '.join(o.rule_key for o in errored)}"
            if errored
            else None
        )
        with conn.begin():
            db.pin_recorded_session(conn, session_settings)
            _close_run(conn, run_id, status, detail)

    return RuleRunResult(
        rule_run_id=run_id,
        correlation_id=correlation_id,
        scope_key=resolved.scope_key,
        as_of_date=resolved.as_of_date,
        reference_watermark=resolved.reference_watermark,
        status=status,
        outcomes=outcomes,
    )
