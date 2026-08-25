"""SC-001 — the produced finding set equals the expected finding set (T033).

**Set equality, not a count and not a subset.** A count passes when one rule over-fires and another
under-fires by the same amount. A subset check passes when the engine invents findings nobody
injected. Only equality catches both, and both are the failures that matter: a steward who sees a
finding on correct data stops trusting the ones on incorrect data.

The expected set comes from the generator, emitted as each defect is injected
(:mod:`dq.seed.defects`). It is never hand-maintained — a hand-written list is a second source of
truth, and when it drifts the symptom is this test failing for reasons that have nothing to do with
the engine.

Three scopes are evaluated, because the nine families do not all answer to the same question:

* the target batch — every ``record`` rule
* the target source and period — ``VOL-DEVIATION``
* the *absent* feed's source and period — ``FEED-LATE-MISSING``, which has no batch at all
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, SourcePeriodScope, run_rules
from dq.seed.generator import MISSING_FEED_SOURCE, TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

_PRODUCED = text("""
SELECT r.rule_key, f.subject_key
FROM   finding f
JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
JOIN   rule r          ON r.rule_id          = rv.rule_id
""")


@pytest.fixture(scope="module")
def evaluated(settings: Settings, seeded: SeedResult, registered: list[str]) -> SeedResult:
    """Run every scope the defect set spans. Returns the seed result for its expectations."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    batch_run = run_rules(settings, BatchScope(batch.batch_id))
    assert batch_run.status != "FAILED", batch_run.status

    for source in (TARGET_SOURCE, MISSING_FEED_SOURCE):
        period_run = run_rules(
            settings, SourcePeriodScope(source, TARGET_PERIOD[0], TARGET_PERIOD[1])
        )
        assert period_run.status != "FAILED", period_run.status

    return seeded


@pytest.fixture(scope="module")
def produced(findings_reader: Connection, evaluated: SeedResult) -> set[tuple[str, str]]:
    return {(row.rule_key, row.subject_key) for row in findings_reader.execute(_PRODUCED)}


def test_finding_set_equals_expected_set(
    produced: set[tuple[str, str]], evaluated: SeedResult
) -> None:
    """SC-001, stated exactly.

    The two diffs are reported separately because they mean opposite things: a missing finding is a
    rule that failed to detect an injected defect, and an unexpected one is a rule firing on data
    that is correct.
    """
    expected = evaluated.expected
    missing = expected - produced
    unexpected = produced - expected

    def render(pairs: set[tuple[str, str]]) -> str:
        return (
            "\n".join(
                f"    {k:<20} {s:<28} {evaluated.notes.get((k, s), '')}" for k, s in sorted(pairs)
            )
            or "    (none)"
        )

    assert not missing and not unexpected, (
        f"\nNOT DETECTED — an injected defect the engine missed:\n{render(missing)}\n"
        f"\nFALSE POSITIVE — a finding on data nobody broke:\n{render(unexpected)}\n"
    )


def test_every_rule_family_is_represented(produced: set[tuple[str, str]]) -> None:
    """Nine families, each with at least one finding.

    Set equality alone would pass if a family were absent from *both* sides — a rule that silently
    evaluates nothing looks identical to a rule with nothing to find.
    """
    families = {key for key, _ in produced}
    assert len(families) == 9, (
        f"only {len(families)} families produced findings: {sorted(families)}"
    )


def test_every_record_finding_carries_its_offending_value(findings_reader: Connection) -> None:
    """SC-002. The CHECK constraint enforces this, so a failure here means the constraint is gone."""
    orphaned = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding WHERE subject_type = 'record' AND offending_value IS NULL"
        )
    ).scalar_one()
    assert orphaned == 0


def test_aggregate_findings_carry_no_batch(findings_reader: Connection) -> None:
    """The other half of the same CHECK: an aggregate subject has no batch to point at.

    A feed that never arrived is the motivating case — it has no batch by definition, which is why
    ``finding.batch_id`` is nullable and why the uniqueness key is on ``scope_key`` instead.
    """
    rows = findings_reader.execute(
        text(
            "SELECT subject_type, batch_id IS NULL AS no_batch FROM finding "
            "WHERE subject_type <> 'record'"
        )
    ).all()
    assert rows, "no aggregate findings were produced at all"
    assert all(r.no_batch for r in rows)


def test_run_recorded_the_world_it_saw(findings_reader: Connection) -> None:
    """Every run pins the business world, the delivered world, and the execution world.

    Without all three recorded, two runs can only be *assumed* equal rather than compared — which is
    what made the reproducibility claim false in the first design (research.md D3).
    """
    runs = findings_reader.execute(
        text(
            "SELECT as_of_date, reference_watermark, session_settings, correlation_id, status "
            "FROM rule_run ORDER BY rule_run_id"
        )
    ).all()
    assert runs, "no rule_run rows"
    for run in runs:
        assert run.as_of_date is not None
        assert run.reference_watermark > 0
        assert run.correlation_id is not None
        assert set(run.session_settings) >= {
            "TimeZone",
            "DateStyle",
            "search_path",
            "statement_timeout",
        }
        assert run.status in ("COMPLETED", "COMPLETED_WITH_ERRORS")


def test_no_rule_errored(findings_reader: Connection) -> None:
    """A rule that errors contributes zero findings for the whole scope, and set equality would then
    fail with a confusing diff. Surfacing the error itself gives a usable message instead."""
    errored = findings_reader.execute(
        text(
            "SELECT r.rule_key, rrv.error_detail FROM rule_run_rule_version rrv "
            "JOIN rule_version rv ON rv.rule_version_id = rrv.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id WHERE rrv.outcome = 'ERRORED'"
        )
    ).all()
    assert not errored, "\n".join(f"{r.rule_key}: {r.error_detail}" for r in errored)
