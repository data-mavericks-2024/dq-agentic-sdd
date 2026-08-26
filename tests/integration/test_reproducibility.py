"""A historical re-run after later data arrives produces an identical finding set (T060, SC-010).

**This is the test the first design would have failed**, and the justification for every piece of
machinery that makes this schema more complex than a naive one: append-only master version chains,
``as_of_date``, and ``reference_watermark``.

The first design argued that batch immutability was sufficient — a batch cannot change, a rule
version cannot change, therefore the result cannot change. That premise is false for four of the
nine rule families, because they join the subject batch to **master data that is not batch-scoped
and grows with every later delivery**. A re-run months later would see a different reference world
and produce a different finding set, while every test passed, because tests re-run within the same
minute against the same world.

So this file has to do something no other test does: it deliberately moves the world on, then asks
for the past back.

Three assertions, and the third is what stops the other two being vacuous:

1. Replaying a run's recorded parameters reproduces its finding set exactly.
2. The persisted findings are unchanged.
3. **The same predicate under a *fresh* watermark returns something different.** Without this, a
   test that pinned nothing would pass just as well, and the machinery would be unfalsifiable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, run_rules
from dq.seed.generator import (
    AMENDED_ORPHAN_KEY,
    TARGET_PERIOD,
    TARGET_SOURCE,
    Amendment,
    SeedResult,
    amend_master,
)

pytestmark = pytest.mark.usefixtures("registered")

#: The last date *inside* the target period, matching what the engine derives from a batch scope.
#: Computed here rather than asked of the database — `DATE :param` is literal-prefix syntax and
#: cannot take a bind parameter, and a round trip to Singapore to subtract a day is absurd anyway.
BUSINESS_END = TARGET_PERIOD[1] - timedelta(days=1)

#: The orphan-reference predicate, reduced to the question this test asks of it: is the ghost
#: transaction still failing? Run directly rather than through the engine, because `ON CONFLICT DO
#: NOTHING` would hide a changed answer behind an unchanged findings table (research.md D11).
_ORPHAN_PROBE = text("""
SELECT count(*) AS n
FROM   sales_transaction t
WHERE  t.batch_id = :batch_id
  AND  t.hcp_key  = :hcp_key
  AND  NOT EXISTS (
         SELECT 1 FROM hcp h
         WHERE  h.source_system_id = t.source_system_id
           AND  h.source_key       = t.hcp_key
           AND  h.valid_from      <= :as_of_date
           AND  h.batch_id        <= :reference_watermark
           AND  NOT h.is_deleted
       )
""")

_FINDINGS = text("""
SELECT r.rule_key, f.subject_key
FROM   finding f
JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
JOIN   rule r          ON r.rule_id          = rv.rule_id
WHERE  f.scope_key = :scope_key
""")


@pytest.fixture(scope="module")
def original_run(settings: Settings, seeded: SeedResult, registered: list[str]) -> int:
    """Evaluate the target batch before anything else has been delivered. Returns the run id."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status == "COMPLETED", result.status
    return result.rule_run_id


@pytest.fixture(scope="module")
def amendment(settings: Settings, seeded: SeedResult, original_run: int) -> Amendment:
    """Deliver a later batch carrying back-dated master versions.

    Ordered after ``original_run`` deliberately: the point is that the amendment lands *after* the
    run whose world we later ask to see again.
    """
    return amend_master(settings, seeded)


@pytest.fixture(scope="module")
def probe(findings_reader: Connection, seeded: SeedResult) -> Callable[[object, int], int]:
    """Run the orphan predicate under an explicit as-of date and watermark."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    def _probe(as_of: object, watermark: int) -> int:
        return int(
            findings_reader.execute(
                _ORPHAN_PROBE,
                {
                    "batch_id": batch.batch_id,
                    "hcp_key": AMENDED_ORPHAN_KEY,
                    "as_of_date": as_of,
                    "reference_watermark": watermark,
                },
            ).scalar_one()
        )

    return _probe


# ---------------------------------------------------------------------------
# The world moved on. Prove it, or the rest of this file proves nothing.
# ---------------------------------------------------------------------------


def test_a_fresh_watermark_sees_a_different_world(
    findings_reader: Connection,
    amendment: Amendment,
    probe: Callable[[object, int], int],
) -> None:
    """The amendment must actually change what the predicate returns.

    If it did not, every assertion below would hold trivially and the reproducibility guarantee
    would be untested. The back-dated HCP resolves the orphan, so under a watermark that includes
    the amendment batch the transaction stops failing.
    """
    before = probe(BUSINESS_END, amendment.watermark_before)
    after = probe(BUSINESS_END, amendment.batch_id)

    assert before == 1, (
        "the ghost transaction should be an orphan under the original watermark; "
        "if it is not, the amendment is testing nothing"
    )
    assert after == 0, (
        "the back-dated master record should resolve the orphan under a fresh watermark. "
        "It did not, so this test cannot distinguish a pinned world from an unpinned one."
    )


def test_the_business_date_alone_would_not_have_saved_us(
    findings_reader: Connection, amendment: Amendment, probe: Callable[[object, int], int]
) -> None:
    """`as_of_date` on its own admits the amendment, because it is back-dated *into* the period.

    This is the precise reason there are two parameters rather than one. A design that pinned only
    the business date would call the amended world a faithful reproduction of the historical one.
    """
    # Unbounded watermark: every delivery visible, business date still pinned.
    unbounded = probe(BUSINESS_END, 10**9)
    assert unbounded == 0, (
        "with only the business date pinned, the back-dated amendment is visible — which is why "
        "reference_watermark exists as a second, independent bound"
    )


# ---------------------------------------------------------------------------
# SC-010 proper.
# ---------------------------------------------------------------------------


def test_replaying_a_run_reproduces_its_finding_set(
    settings: Settings,
    seeded: SeedResult,
    findings_reader: Connection,
    original_run: int,
    amendment: Amendment,
) -> None:
    """Re-run the historical scope with the original run's recorded world. SC-010.

    The replay produces a *new* ``rule_run`` — audit rows are never overwritten — but it must see
    the same world and reach the same conclusions.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    scope_key = f"b:{batch.batch_id}"

    before = {
        (r.rule_key, r.subject_key)
        for r in findings_reader.execute(_FINDINGS, {"scope_key": scope_key})
    }
    assert before, "the original run produced no findings; nothing to reproduce"

    replay = run_rules(settings, BatchScope(batch.batch_id), replay_of=original_run)
    assert replay.status == "COMPLETED", replay.status
    assert replay.rule_run_id != original_run, "a replay must be recorded as its own run"

    after = {
        (r.rule_key, r.subject_key)
        for r in findings_reader.execute(_FINDINGS, {"scope_key": scope_key})
    }

    assert after == before, (
        f"\nreplay changed the finding set for {scope_key}\n"
        f"  appeared: {sorted(after - before)}\n"
        f"  vanished: {sorted(before - after)}\n"
    )


def test_the_replay_recorded_the_same_world(
    findings_reader: Connection, original_run: int, amendment: Amendment
) -> None:
    """Both runs must carry identical pinned parameters, or "identical findings" is a coincidence.

    A replay that reproduced the finding set while recording a different watermark would mean the
    amendment happened not to matter for these rules — true today, and no guarantee at all.
    """
    runs = findings_reader.execute(
        text(
            "SELECT rule_run_id, as_of_date, reference_watermark "
            "FROM rule_run WHERE scope_type = 'batch' ORDER BY rule_run_id"
        )
    ).all()

    original = next(r for r in runs if r.rule_run_id == original_run)
    later = [r for r in runs if r.rule_run_id > original_run]
    assert later, "no replay run was recorded"
    replay = later[-1]

    assert replay.as_of_date == original.as_of_date
    assert replay.reference_watermark == original.reference_watermark
    assert original.reference_watermark < amendment.batch_id, (
        "the original run's watermark should predate the amendment batch, or the replay is not "
        "reaching back past anything"
    )


def test_a_fresh_run_is_allowed_to_differ(
    settings: Settings, seeded: SeedResult, findings_reader: Connection, amendment: Amendment
) -> None:
    """Reproducibility is not immutability of the present.

    A run *without* ``--replay-of`` should see the amended world and record a higher watermark.
    Findings already persisted stay — the table is append-only — but the run's own parameters must
    show it evaluated a later world. Conflating the two would make the system unable to notice new
    data at all.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    fresh = run_rules(settings, BatchScope(batch.batch_id))

    assert fresh.reference_watermark >= amendment.batch_id, (
        "a run with no replay must see the delivered world as it is now"
    )
