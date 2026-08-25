"""A declared feed that delivered nothing is reported (T033a, SC-009, FR-021d).

This is the finding the first design could not record at all. ``finding.batch_id`` was NOT NULL and
runs were scoped to a batch — so the one thing a steward most needs to know, *nothing arrived*, was
the one thing the system could not say. The quickstart papered over it by running the feed rule
against an unrelated batch, which is not the same question.

Worse, ``batch_id`` sat in the uniqueness key, so the same missing period would have re-fired as a
fresh finding against every subsequent batch, breaking FR-011 for the entire aggregate family.
Re-keying on ``scope_key`` is what fixed both, and the idempotency test below is what proves it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import SourcePeriodScope, run_rules
from dq.seed.generator import MISSING_FEED_SOURCE, TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")


@pytest.fixture(scope="module")
def missing_feed_scope() -> SourcePeriodScope:
    return SourcePeriodScope(MISSING_FEED_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1])


def test_a_feed_that_never_delivered_produces_a_finding(
    settings: Settings,
    seeded: SeedResult,
    registered: list[str],
    missing_feed_scope: SourcePeriodScope,
    findings_reader: Connection,
) -> None:
    """The feed is declared, the period passed, and no batch exists for it.

    Not an empty batch — nothing at all. Those are different facts, and FR-021 has to tell them
    apart: an empty delivery means the source had nothing to send, and a missing one means the
    pipeline is broken.
    """
    result = run_rules(settings, missing_feed_scope)
    assert result.status == "COMPLETED", result.status

    rows = findings_reader.execute(
        text(
            "SELECT f.subject_key, f.observed_value, f.batch_id, f.scope_key, f.subject_type "
            "FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = 'FEED-LATE-MISSING'"
        )
    ).all()

    assert len(rows) == 1, f"expected exactly one missing-feed finding, got {len(rows)}"
    finding = rows[0]
    assert finding.subject_key.startswith(MISSING_FEED_SOURCE)
    assert finding.subject_type == "source_period"
    assert finding.batch_id is None, "an aggregate finding must not claim a batch it has none of"
    assert finding.scope_key == f"sp:{MISSING_FEED_SOURCE}:{TARGET_PERIOD[0]:%Y-%m}"
    assert finding.observed_value == "never delivered"


def test_the_feed_that_did_deliver_produces_nothing(
    settings: Settings, registered: list[str], findings_reader: Connection
) -> None:
    """The complement, and the half that makes the test above meaningful.

    Without it, a rule that fired for every declared feed regardless of delivery would pass.
    """
    run_rules(settings, SourcePeriodScope(TARGET_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1]))

    delivered = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = 'FEED-LATE-MISSING' AND f.subject_key LIKE :prefix"
        ),
        {"prefix": f"{TARGET_SOURCE}:%"},
    ).scalar_one()
    assert delivered == 0


def test_re_running_the_same_scope_adds_nothing(
    settings: Settings,
    registered: list[str],
    missing_feed_scope: SourcePeriodScope,
    findings_reader: Connection,
) -> None:
    """FR-011 for the aggregate family, which is where the first design broke.

    Idempotency here is a database constraint — ``UNIQUE (rule_version_id, scope_key, subject_key)``
    plus ``ON CONFLICT DO NOTHING`` — not an application-side check, so it survives a run killed
    mid-flight.
    """

    def count() -> int:
        return int(
            findings_reader.execute(
                text(
                    "SELECT count(*) FROM finding f "
                    "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
                    "JOIN rule r ON r.rule_id = rv.rule_id "
                    "WHERE r.rule_key = 'FEED-LATE-MISSING'"
                )
            ).scalar_one()
        )

    before = count()
    run_rules(settings, missing_feed_scope)
    run_rules(settings, missing_feed_scope)
    assert count() == before, "re-running a scope duplicated an aggregate finding"
