"""A never-arrived feed appears in its own summary, not only in a findings query (T066a, FR-024a,
SC-009).

`test_missing_feed.py` proves the finding itself is recorded. This proves the aggregate view a
steward actually reads — the summary — does not silently drop it. FR-024a's own framing is the
reason this matters: nothing about a missing feed *looks* wrong to a batch-shaped view, because
there is no batch. The numbers simply go quiet, and a summary is where "quiet" has to become
visible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection

from dq.config.settings import Settings
from dq.engine.runner import SourcePeriodScope, run_rules
from dq.engine.summary import summarise_scope
from dq.seed.generator import MISSING_FEED_SOURCE, TARGET_PERIOD, SeedResult

pytestmark = pytest.mark.usefixtures("registered")


@pytest.fixture(scope="module")
def evaluated_missing_period(settings: Settings, seeded: SeedResult, registered: list[str]) -> None:
    result = run_rules(
        settings, SourcePeriodScope(MISSING_FEED_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1])
    )
    assert result.status == "COMPLETED", result.status


def test_the_missing_feed_appears_in_its_own_summary(
    findings_reader: Connection, evaluated_missing_period: None
) -> None:
    scope = SourcePeriodScope(MISSING_FEED_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1])
    summary = summarise_scope(findings_reader, scope)

    expected_scope_key = f"sp:{MISSING_FEED_SOURCE}:{TARGET_PERIOD[0]:%Y-%m}"
    assert summary.scope_keys == (expected_scope_key,)
    assert summary.total_findings >= 1
    assert any(rc.rule_key == "FEED-LATE-MISSING" for rc in summary.by_rule)
    assert sum(summary.by_domain.values()) == summary.total_findings
    assert sum(summary.by_severity.values()) == summary.total_findings


def test_a_source_period_scope_has_no_batch_to_reach_beyond(
    findings_reader: Connection, evaluated_missing_period: None
) -> None:
    """The augmentation FR-024a describes is one-directional: a *batch* summary reaches out to its
    matching `source_period` findings. A `source_period` scope has no batch of its own to add, so
    exactly one scope key is ever queried here — never a fabricated `b:` key."""
    scope = SourcePeriodScope(MISSING_FEED_SOURCE, TARGET_PERIOD[0], TARGET_PERIOD[1])
    summary = summarise_scope(findings_reader, scope)

    assert len(summary.scope_keys) == 1
