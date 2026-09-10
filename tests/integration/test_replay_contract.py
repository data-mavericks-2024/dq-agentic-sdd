"""Persisted execution-context and rule-version contract for replay (T081)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine import runner
from dq.rules.definition import RuleDefinition
from dq.rules.registry import register, set_active
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

RULE_KEY = "TEST-REPLAY-PINNED-VERSION"
PREDICATE = """
SELECT h.hcp_id::text              AS subject_key,
       coalesce(h.npi, '<absent>') AS offending_value,
       CAST(:label AS text)         AS observed_value,
       CAST(:label AS text)         AS expected_value
FROM   hcp h
WHERE  h.batch_id    = :batch_id
  AND  h.valid_from <= :as_of_date
  AND  h.batch_id   <= :reference_watermark
  AND  NOT h.is_deleted
  AND  h.npi IS NULL
"""


def _explicit_replay(settings: Settings, source_id: int) -> runner.RuleRunResult:
    run_rules = cast(Callable[..., runner.RuleRunResult], runner.run_rules)
    return run_rules(settings, replay_of=source_id)


def _replay_source_error() -> type[BaseException]:
    value = getattr(runner, "ReplaySourceError", None)
    assert isinstance(value, type), "dq.engine.runner must export ReplaySourceError"
    assert issubclass(value, BaseException)
    return value


def _definition(label: str) -> RuleDefinition:
    return RuleDefinition.model_validate(
        {
            "rule_key": RULE_KEY,
            "domain": "HCP",
            "dimension": "validity",
            "severity": "MEDIUM",
            "owning_function": "Master Data Management",
            "subject_type": "record",
            "predicate_sql": PREDICATE,
            "parameters": {"label": label},
        }
    )


def _register(settings: Settings, label: str) -> Any:
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        return register(conn, _definition(label))


def _set_active(settings: Settings, active: bool) -> None:
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        assert set_active(conn, RULE_KEY, active)


@pytest.fixture(scope="module")
def replay_case(
    settings: Settings, seeded: SeedResult, registered: list[str]
) -> Iterator[dict[str, Any]]:
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    version_one = _register(settings, "version-one")
    original = runner.run_rules(settings, runner.BatchScope(batch.batch_id), rule_keys=[RULE_KEY])
    assert original.status == "COMPLETED"
    assert original.finding_count > 0, "the replay fixture needs a persisted conflict to prove zero"

    version_two = _register(settings, "version-two")
    _set_active(settings, False)
    try:
        yield {
            "batch_id": batch.batch_id,
            "original": original,
            "version_one": version_one,
            "version_two": version_two,
        }
    finally:
        _set_active(settings, False)


def _run_row(conn: Connection, run_id: int) -> Any:
    return conn.execute(
        text(
            "SELECT scope_type, batch_id, scope_source_system_id, scope_period, as_of_date, "
            "reference_watermark, session_settings, status, replay_of_rule_run_id "
            "FROM rule_run WHERE rule_run_id = :id"
        ),
        {"id": run_id},
    ).one()


def _recorded_versions(conn: Connection, run_id: int) -> list[Any]:
    return list(
        conn.execute(
            text(
                "SELECT rule_version_id, outcome, finding_count, error_detail "
                "FROM rule_run_rule_version WHERE rule_run_id = :id ORDER BY rule_version_id"
            ),
            {"id": run_id},
        ).all()
    )


def test_fresh_head_has_nullable_restrictive_replay_lineage(
    findings_reader: Connection,
) -> None:
    column = findings_reader.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = ("
            "  SELECT namespace_row.nspname FROM pg_class table_row "
            "  JOIN pg_namespace namespace_row ON namespace_row.oid = table_row.relnamespace "
            "  WHERE table_row.oid = 'rule_run'::regclass"
            ") AND table_name = 'rule_run' "
            "AND column_name = 'replay_of_rule_run_id'"
        )
    ).scalar_one()
    foreign_key = findings_reader.execute(
        text("""
            SELECT target.relname AS target_table, constraint_row.confdeltype
            FROM pg_constraint constraint_row
            JOIN pg_class source ON source.oid = constraint_row.conrelid
            JOIN pg_namespace namespace_row ON namespace_row.oid = source.relnamespace
            JOIN pg_class target ON target.oid = constraint_row.confrelid
            WHERE source.oid = 'rule_run'::regclass
              AND constraint_row.contype = 'f'
              AND pg_get_constraintdef(constraint_row.oid)
                  LIKE 'FOREIGN KEY (replay_of_rule_run_id)%'
        """)
    ).one()

    assert column == "YES"
    assert foreign_key.target_table == "rule_run"
    assert foreign_key.confdeltype == "r"

    unique_constraints = findings_reader.execute(
        text("""
            SELECT array_agg(attribute_row.attname ORDER BY key_column.position) AS columns
            FROM pg_constraint constraint_row
            JOIN pg_class table_row ON table_row.oid = constraint_row.conrelid
            CROSS JOIN LATERAL
                 unnest(constraint_row.conkey) WITH ORDINALITY
                 AS key_column(attribute_number, position)
            JOIN pg_attribute attribute_row
              ON attribute_row.attrelid = table_row.oid
             AND attribute_row.attnum = key_column.attribute_number
            WHERE table_row.oid = 'finding'::regclass
              AND constraint_row.contype = 'u'
            GROUP BY constraint_row.oid
        """)
    ).scalars()
    assert (
        "rule_version_id",
        "scope_key",
        "subject_key",
    ) in {tuple(columns) for columns in unique_constraints}


def test_replay_copies_complete_context_and_recorded_version_even_when_inactive(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    findings_reader: Connection,
    replay_case: dict[str, Any],
) -> None:
    original_result = replay_case["original"]
    original = _run_row(findings_reader, original_result.rule_run_id)
    monkeypatch.setitem(db.SESSION_SETTINGS, "statement_timeout", "14min")
    replay_result = _explicit_replay(settings, original_result.rule_run_id)

    replay = _run_row(findings_reader, replay_result.rule_run_id)
    original_versions = _recorded_versions(findings_reader, original_result.rule_run_id)
    replay_versions = _recorded_versions(findings_reader, replay_result.rule_run_id)

    assert replay_result.status == "COMPLETED"
    assert replay_result.rule_run_id != original_result.rule_run_id
    assert replay.replay_of_rule_run_id == original_result.rule_run_id
    assert original.replay_of_rule_run_id is None
    assert replay.scope_type == original.scope_type
    assert replay.batch_id == original.batch_id
    assert replay.scope_source_system_id == original.scope_source_system_id
    assert replay.scope_period == original.scope_period
    assert replay.as_of_date == original.as_of_date
    assert replay.reference_watermark == original.reference_watermark
    assert replay.session_settings == original.session_settings
    assert [row.rule_version_id for row in replay_versions] == [
        row.rule_version_id for row in original_versions
    ]
    assert replay_versions[0].rule_version_id == replay_case["version_one"].rule_version_id
    assert replay_versions[0].outcome == "EVALUATED"
    assert replay_versions[0].finding_count == 0
    assert replay_result.finding_count == 0


def test_fresh_evaluation_uses_the_current_active_version(
    settings: Settings, findings_reader: Connection, replay_case: dict[str, Any]
) -> None:
    _set_active(settings, True)
    try:
        fresh = runner.run_rules(
            settings,
            runner.BatchScope(replay_case["batch_id"]),
            rule_keys=[RULE_KEY],
        )
    finally:
        _set_active(settings, False)

    row = _run_row(findings_reader, fresh.rule_run_id)
    versions = _recorded_versions(findings_reader, fresh.rule_run_id)

    assert row.replay_of_rule_run_id is None
    assert [version.rule_version_id for version in versions] == [
        replay_case["version_two"].rule_version_id
    ]


def _insert_source_with_status(settings: Settings, original_id: int, status: str) -> int:
    with db.connect(settings, Role.ENGINE) as conn:
        return int(
            conn.execute(
                text("""
                    INSERT INTO rule_run (
                        correlation_id, scope_type, batch_id, scope_source_system_id,
                        scope_period, as_of_date, reference_watermark, session_settings,
                        started_at, finished_at, status, error_detail
                    )
                    SELECT :correlation_id, scope_type, batch_id, scope_source_system_id,
                           scope_period, as_of_date, reference_watermark, session_settings,
                           started_at, finished_at, :status, error_detail
                    FROM rule_run
                    WHERE rule_run_id = :original_id
                    RETURNING rule_run_id
                """),
                {
                    "correlation_id": uuid.uuid4(),
                    "status": status,
                    "original_id": original_id,
                },
            ).scalar_one()
        )


@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "COMPLETED_WITH_ERRORS"])
def test_noncompleted_replay_source_is_rejected_without_creating_a_run(
    settings: Settings,
    findings_reader: Connection,
    replay_case: dict[str, Any],
    status: str,
) -> None:
    source_id = _insert_source_with_status(settings, replay_case["original"].rule_run_id, status)
    before = findings_reader.execute(text("SELECT count(*) FROM rule_run")).scalar_one()

    with pytest.raises(_replay_source_error(), match=status):
        _explicit_replay(settings, source_id)

    after = findings_reader.execute(text("SELECT count(*) FROM rule_run")).scalar_one()
    assert after == before


def test_unknown_replay_source_is_rejected_without_creating_a_run(
    settings: Settings, findings_reader: Connection
) -> None:
    missing_id = int(
        findings_reader.execute(
            text("SELECT coalesce(max(rule_run_id), 0) + 1000 FROM rule_run")
        ).scalar_one()
    )
    before = findings_reader.execute(text("SELECT count(*) FROM rule_run")).scalar_one()

    with pytest.raises(_replay_source_error(), match=str(missing_id)):
        _explicit_replay(settings, missing_id)

    after = findings_reader.execute(text("SELECT count(*) FROM rule_run")).scalar_one()
    assert after == before
