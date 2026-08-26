"""A run killed mid-flight and re-executed yields the same finding set (T061, principle IX).

Recovery here is not a mechanism — it is the *absence* of one. There is no resume log, no
checkpoint, no partial-state table. Re-running is the recovery, because the uniqueness constraint
makes a second run over the same scope converge on the same rows regardless of how far the first
one got.

That property is worth testing precisely because nothing implements it. It follows from two
decisions made elsewhere — the unique key and ``ON CONFLICT DO NOTHING`` — and a change to either
would break it silently, with no failing code path to notice.

The interruption is simulated by evaluating a **subset of rules**, which is exactly what a run
killed between statements leaves behind: some rules' findings persisted, the rest absent, and a
``rule_run`` row still marked ``RUNNING``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, run_rules
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

#: Roughly half the record-level library. Standing in for "the run died after these".
PARTIAL_RULES = ["HCP-NPI-FORMAT", "HCP-DUP-NPI", "SALES-ORPHAN-REF"]

_FINDINGS = text("""
SELECT r.rule_key, f.subject_key
FROM   finding f
JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
JOIN   rule r          ON r.rule_id          = rv.rule_id
WHERE  f.scope_key = :scope_key
""")


@pytest.fixture(scope="module")
def uninterrupted(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> set[tuple[str, str]]:
    """The finding set a clean run produces. The target every interrupted path must reach."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    run_rules(settings, BatchScope(batch.batch_id))
    return {
        (r.rule_key, r.subject_key)
        for r in findings_reader.execute(_FINDINGS, {"scope_key": f"b:{batch.batch_id}"})
    }


def test_a_partial_run_followed_by_a_full_one_converges(
    settings: Settings,
    seeded: SeedResult,
    findings_reader: Connection,
    uninterrupted: set[tuple[str, str]],
) -> None:
    """Half the rules, then all of them. The result is indistinguishable from one clean run."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    scope_key = f"b:{batch.batch_id}"

    run_rules(settings, BatchScope(batch.batch_id), rule_keys=PARTIAL_RULES)
    run_rules(settings, BatchScope(batch.batch_id))

    recovered = {
        (r.rule_key, r.subject_key)
        for r in findings_reader.execute(_FINDINGS, {"scope_key": scope_key})
    }
    assert recovered == uninterrupted, (
        f"recovery diverged from a clean run\n"
        f"  extra:   {sorted(recovered - uninterrupted)}\n"
        f"  missing: {sorted(uninterrupted - recovered)}\n"
    )


def test_an_abandoned_run_row_does_not_block_recovery(
    settings: Settings, seeded: SeedResult, findings_reader: Connection
) -> None:
    """A `rule_run` left `RUNNING` by a killed process must not stop the next run.

    There is no lock to clear and no state to reconcile: the advisory lock is transaction-scoped, so
    it dies with the connection. A design that had recorded resumable progress would need a
    reconciliation path here, and that path would be the thing most likely to be wrong.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    # A run that never closed. Written as dq_engine, which is the role that would have left it.
    stranded = findings_reader.execute(
        text("SELECT count(*) FROM rule_run WHERE batch_id = :b AND status = 'RUNNING'"),
        {"b": batch.batch_id},
    ).scalar_one()

    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status == "COMPLETED", (
        f"a fresh run refused to complete with {stranded} stranded RUNNING row(s) present"
    )


def test_every_rule_is_recorded_even_when_it_finds_nothing(
    settings: Settings, seeded: SeedResult, findings_reader: Connection
) -> None:
    """`rule_run_rule_version` must show every rule that was evaluated, not only those that fired.

    This is what makes an interrupted run *detectable*. Without a row per rule, "this rule found
    nothing" and "this rule never ran" are the same observation — and a steward reading a summary
    could not tell a clean batch from a half-checked one.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))

    evaluated = findings_reader.execute(
        text("SELECT count(*) FROM rule_run_rule_version WHERE rule_run_id = :r"),
        {"r": result.rule_run_id},
    ).scalar_one()

    assert evaluated == len(result.outcomes)
    assert evaluated > 0, "the run recorded no per-rule outcomes at all"
