"""Re-running a scope creates no duplicate findings (T059, SC-003, FR-011).

Idempotency here is a **database constraint**, not an application check:
``UNIQUE (rule_version_id, scope_key, subject_key)`` plus ``ON CONFLICT DO NOTHING``. That
distinction is the whole point — a check in the engine protects only the paths that remember to call
it, and cannot survive a run killed between statements.

Note carefully what this does *not* prove. Non-duplication is not reproducibility. The constraint
guarantees a second run adds nothing; it says nothing about whether the second run would have
produced the same rows, because ``ON CONFLICT DO NOTHING`` silently discards a differing row for a
subject that already exists. Conflating the two was the first design's central error, and
``test_reproducibility.py`` is where identity is actually established (research.md D3, D11).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, SourcePeriodScope, run_rules
from dq.seed.generator import MISSING_FEED_SOURCE, TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

_COUNT_FOR_SCOPE = text("SELECT count(*) FROM finding WHERE scope_key = :scope_key")

_ROWS_FOR_SCOPE = text("""
SELECT f.finding_id, f.rule_version_id, f.subject_key, f.offending_value, f.detected_at
FROM   finding f
WHERE  f.scope_key = :scope_key
ORDER  BY f.finding_id
""")


def test_re_running_a_batch_scope_adds_nothing(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> None:
    """Three runs, one finding set."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    scope_key = f"b:{batch.batch_id}"

    run_rules(settings, BatchScope(batch.batch_id))
    first = findings_reader.execute(_COUNT_FOR_SCOPE, {"scope_key": scope_key}).scalar_one()
    assert first > 0, "nothing was detected, so this test would pass vacuously"

    run_rules(settings, BatchScope(batch.batch_id))
    run_rules(settings, BatchScope(batch.batch_id))
    third = findings_reader.execute(_COUNT_FOR_SCOPE, {"scope_key": scope_key}).scalar_one()

    assert third == first, f"re-running duplicated findings: {first} → {third}"


def test_re_running_leaves_the_original_rows_untouched(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> None:
    """Not merely the same count — the same rows, with their original ids and timestamps.

    A count-only assertion would pass if one finding were somehow replaced by another. ``finding``
    is immutable by trigger and ``detected_at`` is stamped at insert, so a changed timestamp would
    mean a row was deleted and re-inserted, which the trigger should have refused.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    scope_key = f"b:{batch.batch_id}"

    run_rules(settings, BatchScope(batch.batch_id))
    before = [tuple(r) for r in findings_reader.execute(_ROWS_FOR_SCOPE, {"scope_key": scope_key})]

    run_rules(settings, BatchScope(batch.batch_id))
    after = [tuple(r) for r in findings_reader.execute(_ROWS_FOR_SCOPE, {"scope_key": scope_key})]

    assert after == before, "a re-run altered rows that should have been left alone"


def test_re_running_an_aggregate_scope_adds_nothing(
    settings: Settings, registered: list[str], findings_reader: Connection
) -> None:
    """The aggregate family is where the first design's uniqueness key actually broke.

    With ``batch_id`` in the key and null for every aggregate finding, the same missing period
    would have re-fired as a fresh row against every subsequent batch. Keying on ``scope_key``
    — non-null for all three subject types — is what makes idempotency uniform.
    """
    scope = SourcePeriodScope(MISSING_FEED_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1])
    scope_key = f"sp:{MISSING_FEED_SOURCE}:{TARGET_PERIOD[0]:%Y-%m}"

    run_rules(settings, scope)
    first = findings_reader.execute(_COUNT_FOR_SCOPE, {"scope_key": scope_key}).scalar_one()
    assert first > 0

    run_rules(settings, scope)
    second = findings_reader.execute(_COUNT_FOR_SCOPE, {"scope_key": scope_key}).scalar_one()
    assert second == first


def test_each_run_is_recorded_separately(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> None:
    """Idempotent findings, but not idempotent *runs*.

    Every execution gets its own ``rule_run`` with its own correlation id, because the audit trail
    has to answer "when was this last checked?" as well as "what was found". Collapsing repeat runs
    would lose that, and constitution principle V does not allow an audit row to be overwritten.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    def run_count() -> int:
        return int(
            findings_reader.execute(
                text("SELECT count(*) FROM rule_run WHERE batch_id = :b"), {"b": batch.batch_id}
            ).scalar_one()
        )

    before = run_count()
    run_rules(settings, BatchScope(batch.batch_id))
    assert run_count() == before + 1

    correlations = findings_reader.execute(
        text("SELECT count(DISTINCT correlation_id) FROM rule_run WHERE batch_id = :b"),
        {"b": batch.batch_id},
    ).scalar_one()
    assert correlations == run_count(), "two runs shared a correlation id"
