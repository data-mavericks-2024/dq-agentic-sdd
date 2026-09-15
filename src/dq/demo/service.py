"""Read model and fixed orchestration for the nightly commercial-load demo.

The browser never receives a database credential and never issues SQL. Read paths connect as
``dq_readonly``; the one state-changing action delegates to the existing deterministic runner,
which asserts that it connected as ``dq_engine`` before evaluating any rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, bindparam, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, SourcePeriodScope, run_rules
from dq.rules.definition import LIBRARY_DIR, load_library
from dq.rules.registry import register


class DemoDataError(RuntimeError):
    """The hosted database does not contain a runnable demo world."""


@dataclass(frozen=True, slots=True)
class NightlyBatch:
    batch_id: int
    source_code: str
    source_name: str
    period_start: date
    period_end: date
    record_count: int


@dataclass(frozen=True, slots=True)
class NightlySource:
    source_system_id: int
    code: str
    name: str


class DemoModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchView(DemoModel):
    batch_id: int
    source_code: str
    source_name: str
    record_count: int
    arrival_ts: datetime


class FindingView(DemoModel):
    finding_id: int
    subject_key: str
    subject_type: str
    rule_key: str
    version_no: int
    domain: str
    dimension: str
    severity: str
    owning_function: str
    offending_value: str | None
    observed_value: str | None
    expected_value: str | None
    detected_at: datetime
    rule_run_id: int


class RuleView(DemoModel):
    rule_key: str
    version_no: int
    domain: str
    dimension: str
    severity: str
    owning_function: str
    subject_type: str


class RunView(DemoModel):
    rule_run_id: int
    correlation_id: str
    scope_key: str
    status: str
    reference_watermark: int
    started_at: datetime
    finished_at: datetime | None
    rules_evaluated: int
    rules_errored: int
    new_findings: int
    replay_of_rule_run_id: int | None


class CountView(DemoModel):
    label: str
    count: int


class NightlyState(DemoModel):
    scenario: str = "Nightly commercial load"
    generated_at: datetime
    period: str
    available_periods: list[str]
    source_label: str
    batches: list[BatchView]
    records_evaluated: int
    findings_count: int
    new_findings: int
    rules_evaluated: int
    rules_errored: int
    run_duration_seconds: float
    run_status: str
    reference_watermark: int | None
    dimensions: list[CountView]
    severities: list[CountView]
    findings: list[FindingView]
    rules: list[RuleView]
    runs: list[RunView]
    evidence_status: str


def parse_period(value: str) -> tuple[date, date]:
    """Parse canonical ``YYYY-MM`` into a half-open monthly range."""
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError
        start = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError(f"period must use YYYY-MM; got {value!r}") from exc
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, end


def build_scope_keys(
    period: str,
    batches: list[NightlyBatch],
    sources: list[NightlySource],
) -> tuple[str, ...]:
    """Return every persisted scope represented by one nightly period."""
    keys = {f"b:{batch.batch_id}" for batch in batches}
    keys.update(f"sp:{source.code}:{period}" for source in sources)
    return tuple(sorted(keys))


def _expanding(statement: str, parameter: str) -> Any:
    return text(statement).bindparams(bindparam(parameter, expanding=True))


class NightlyDemoService:
    """Build one management read model from immutable database evidence."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def health(self) -> dict[str, object]:
        with db.connect(self._settings, Role.READONLY) as conn:
            version = str(conn.execute(text("SHOW server_version")).scalar_one())
        return {"status": "healthy", "database": f"PostgreSQL {version}"}

    def _periods(self, conn: Connection) -> list[str]:
        rows = conn.execute(
            text(
                "SELECT DISTINCT to_char(lower(business_period), 'YYYY-MM') AS period "
                "FROM data_batch ORDER BY period DESC"
            )
        ).scalars()
        return [str(row) for row in rows]

    def _sources(self, conn: Connection) -> list[NightlySource]:
        rows = conn.execute(
            text("SELECT source_system_id, code, name FROM source_system ORDER BY code")
        ).mappings()
        return [
            NightlySource(int(row["source_system_id"]), str(row["code"]), str(row["name"]))
            for row in rows
        ]

    def _batches(
        self, conn: Connection, period_start: date, period_end: date
    ) -> tuple[list[NightlyBatch], list[BatchView]]:
        rows = list(
            conn.execute(
                text(
                    "SELECT b.batch_id, s.code, s.name, lower(b.business_period) AS period_start, "
                    "upper(b.business_period) AS period_end, b.record_count, b.arrival_ts "
                    "FROM data_batch b JOIN source_system s USING (source_system_id) "
                    "WHERE lower(b.business_period) = :start AND upper(b.business_period) = :end "
                    "ORDER BY s.code, b.arrival_ts"
                ),
                {"start": period_start, "end": period_end},
            ).mappings()
        )
        batches = [
            NightlyBatch(
                int(row["batch_id"]),
                str(row["code"]),
                str(row["name"]),
                row["period_start"],
                row["period_end"],
                int(row["record_count"]),
            )
            for row in rows
        ]
        views = [
            BatchView(
                batch_id=int(row["batch_id"]),
                source_code=str(row["code"]),
                source_name=str(row["name"]),
                record_count=int(row["record_count"]),
                arrival_ts=row["arrival_ts"],
            )
            for row in rows
        ]
        return batches, views

    def _latest_runs(
        self,
        conn: Connection,
        batch_ids: list[int],
        period_start: date,
        period_end: date,
    ) -> list[dict[str, Any]]:
        statement_sql = (
            """
            SELECT rr.rule_run_id, rr.correlation_id, rr.replay_of_rule_run_id,
                   rr.scope_type, rr.batch_id, source.code AS source_code,
                   lower(rr.scope_period) AS period_start, rr.reference_watermark,
                   rr.status, rr.started_at, rr.finished_at
            FROM rule_run rr
            LEFT JOIN source_system source
                   ON source.source_system_id = rr.scope_source_system_id
            WHERE (rr.scope_type = 'batch' AND rr.batch_id IN :batch_ids)
               OR (rr.scope_type = 'source_period'
                   AND lower(rr.scope_period) = :start AND upper(rr.scope_period) = :end)
            ORDER BY rr.started_at DESC, rr.rule_run_id DESC
            """
            if batch_ids
            else """
            SELECT rr.rule_run_id, rr.correlation_id, rr.replay_of_rule_run_id,
                   rr.scope_type, rr.batch_id, source.code AS source_code,
                   lower(rr.scope_period) AS period_start, rr.reference_watermark,
                   rr.status, rr.started_at, rr.finished_at
            FROM rule_run rr
            LEFT JOIN source_system source
                   ON source.source_system_id = rr.scope_source_system_id
            WHERE rr.scope_type = 'source_period'
              AND lower(rr.scope_period) = :start AND upper(rr.scope_period) = :end
            ORDER BY rr.started_at DESC, rr.rule_run_id DESC
            """
        )
        statement = _expanding(statement_sql, "batch_ids") if batch_ids else text(statement_sql)
        parameters: dict[str, object] = {"start": period_start, "end": period_end}
        if batch_ids:
            parameters["batch_ids"] = batch_ids
        rows = conn.execute(
            statement,
            parameters,
        ).mappings()
        latest: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            scope_key = (
                f"b:{row['batch_id']}"
                if row["scope_type"] == "batch"
                else f"sp:{row['source_code']}:{period_start:%Y-%m}"
            )
            row["scope_key"] = scope_key
            latest.setdefault(scope_key, row)
        return sorted(latest.values(), key=lambda row: row["started_at"], reverse=True)

    def _run_views(self, conn: Connection, rows: list[dict[str, Any]]) -> list[RunView]:
        run_ids = [int(row["rule_run_id"]) for row in rows]
        if not run_ids:
            return []
        outcomes = conn.execute(
            _expanding(
                "SELECT rule_run_id, count(*) AS rules_evaluated, "
                "count(*) FILTER (WHERE outcome = 'ERRORED') AS rules_errored, "
                "coalesce(sum(finding_count), 0) AS new_findings "
                "FROM rule_run_rule_version WHERE rule_run_id IN :run_ids "
                "GROUP BY rule_run_id",
                "run_ids",
            ),
            {"run_ids": run_ids},
        ).mappings()
        by_run = {int(row["rule_run_id"]): row for row in outcomes}
        result: list[RunView] = []
        for row in rows:
            counts: Mapping[str, Any] = dict(by_run.get(int(row["rule_run_id"]), {}))
            result.append(
                RunView(
                    rule_run_id=int(row["rule_run_id"]),
                    correlation_id=str(row["correlation_id"]),
                    replay_of_rule_run_id=(
                        int(row["replay_of_rule_run_id"])
                        if row["replay_of_rule_run_id"] is not None
                        else None
                    ),
                    scope_key=str(row["scope_key"]),
                    status=str(row["status"]),
                    reference_watermark=int(row["reference_watermark"]),
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    rules_evaluated=int(counts.get("rules_evaluated", 0)),
                    rules_errored=int(counts.get("rules_errored", 0)),
                    new_findings=int(counts.get("new_findings", 0)),
                )
            )
        return result

    def _findings(self, conn: Connection, scope_keys: tuple[str, ...]) -> list[FindingView]:
        if not scope_keys:
            return []
        rows = conn.execute(
            _expanding(
                """
                SELECT f.finding_id, f.subject_key, f.subject_type, f.offending_value,
                       f.observed_value, f.expected_value, f.severity, f.detected_at,
                       f.rule_run_id, r.rule_key, r.domain, r.dimension, r.owning_function,
                       rv.version_no
                FROM finding f
                JOIN rule_version rv USING (rule_version_id)
                JOIN rule r USING (rule_id)
                WHERE f.scope_key IN :scope_keys
                ORDER BY CASE f.severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                         f.detected_at DESC, f.finding_id
                LIMIT 500
                """,
                "scope_keys",
            ),
            {"scope_keys": list(scope_keys)},
        ).mappings()
        return [FindingView.model_validate(dict(row)) for row in rows]

    def _counts(
        self, conn: Connection, scope_keys: tuple[str, ...], column: str
    ) -> list[CountView]:
        if not scope_keys:
            return []
        statements = {
            "dimension": (
                "SELECT r.dimension AS label, count(*) AS count FROM finding f "
                "JOIN rule_version rv USING (rule_version_id) "
                "JOIN rule r USING (rule_id) WHERE f.scope_key IN :scope_keys "
                "GROUP BY r.dimension ORDER BY count DESC, label"
            ),
            "severity": (
                "SELECT f.severity AS label, count(*) AS count FROM finding f "
                "JOIN rule_version rv USING (rule_version_id) "
                "JOIN rule r USING (rule_id) WHERE f.scope_key IN :scope_keys "
                "GROUP BY f.severity ORDER BY count DESC, label"
            ),
        }
        if column not in statements:
            raise ValueError("unsupported count column")
        rows = conn.execute(
            _expanding(statements[column], "scope_keys"),
            {"scope_keys": list(scope_keys)},
        ).mappings()
        return [CountView(label=str(row["label"]), count=int(row["count"])) for row in rows]

    def _rules(self, conn: Connection) -> list[RuleView]:
        rows = conn.execute(
            text(
                """
                SELECT r.rule_key, r.domain, r.dimension, r.owning_function,
                       latest.version_no, latest.severity, latest.subject_type
                FROM rule r
                JOIN LATERAL (
                    SELECT rv.version_no, rv.severity, rv.subject_type
                    FROM rule_version rv WHERE rv.rule_id = r.rule_id
                    ORDER BY rv.version_no DESC LIMIT 1
                ) latest ON true
                WHERE r.is_active
                ORDER BY r.domain, r.rule_key
                """
            )
        ).mappings()
        return [RuleView.model_validate(dict(row)) for row in rows]

    def load(self, period: str | None = None) -> NightlyState:
        with db.connect(self._settings, Role.READONLY) as conn:
            periods = self._periods(conn)
            if not periods:
                raise DemoDataError("No demo batches exist. Run `dq demo prepare` first.")
            selected = period or periods[0]
            if selected not in periods:
                raise DemoDataError(f"No commercial batch exists for period {selected}.")
            period_start, period_end = parse_period(selected)
            sources = self._sources(conn)
            batches, batch_views = self._batches(conn, period_start, period_end)
            scope_keys = build_scope_keys(selected, batches, sources)
            run_rows = self._latest_runs(
                conn, [batch.batch_id for batch in batches], period_start, period_end
            )
            runs = self._run_views(conn, run_rows)
            findings = self._findings(conn, scope_keys)
            dimensions = self._counts(conn, scope_keys, "dimension")
            severities = self._counts(conn, scope_keys, "severity")
            rules = self._rules(conn)

        completed = [run for run in runs if run.status == "COMPLETED"]
        status = "NOT RUN"
        if runs:
            status = (
                "COMPLETED_WITH_ERRORS"
                if any(run.status == "COMPLETED_WITH_ERRORS" for run in runs)
                else "COMPLETED"
                if len(completed) == len(runs)
                else "INCOMPLETE"
            )
        duration = sum(
            (run.finished_at - run.started_at).total_seconds()
            for run in runs
            if run.finished_at is not None
        )
        return NightlyState(
            generated_at=datetime.now(UTC),
            period=selected,
            available_periods=periods,
            source_label="All commercial sources",
            batches=batch_views,
            records_evaluated=sum(batch.record_count for batch in batches),
            findings_count=sum(item.count for item in severities),
            new_findings=sum(run.new_findings for run in runs),
            rules_evaluated=min(len(rules), sum(run.rules_evaluated for run in runs)),
            rules_errored=sum(run.rules_errored for run in runs),
            run_duration_seconds=duration,
            run_status=status,
            reference_watermark=max((run.reference_watermark for run in runs), default=None),
            dimensions=dimensions,
            severities=severities,
            findings=findings,
            rules=rules,
            runs=runs,
            evidence_status="Attributable and immutable" if runs else "Awaiting first run",
        )

    def run(self, period: str) -> NightlyState:
        period_start, period_end = parse_period(period)
        with db.connect(self._settings, Role.READONLY) as conn:
            periods = self._periods(conn)
            if period not in periods:
                raise DemoDataError(f"No commercial batch exists for period {period}.")
            sources = self._sources(conn)
            batches, _ = self._batches(conn, period_start, period_end)

        for batch in sorted(batches, key=lambda item: item.batch_id):
            run_rules(self._settings, BatchScope(batch.batch_id))
        for source in sorted(sources, key=lambda item: item.code):
            run_rules(
                self._settings,
                SourcePeriodScope(source.code, period_start, period_end),
            )
        return self.load(period)


def prepare(settings: Settings) -> NightlyState:
    """Prepare one repeatable synthetic demo world and execute its latest nightly period.

    This is an operations command, never an HTTP route. It needs author and ingest capability to
    register the shipped rules and seed an empty database; the serving process needs only the
    readonly and engine role URLs.
    """
    with db.connect(settings, Role.AUTHOR) as conn:
        for definition in load_library(LIBRARY_DIR):
            register(conn, definition)

    with db.connect(settings, Role.READONLY) as conn:
        has_batches = bool(conn.execute(text("SELECT EXISTS (SELECT 1 FROM data_batch)")).scalar())
    if not has_batches:
        from dq.seed.generator import seed

        seed(settings, with_defects=True)

    service = NightlyDemoService(settings)
    current = service.load()
    return service.run(current.period)
