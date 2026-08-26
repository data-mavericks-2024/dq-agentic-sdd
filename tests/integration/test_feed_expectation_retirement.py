"""An end-dated feed expectation stops firing, without losing what it already raised (T055b, FR-021c).

A feed that is decommissioned should stop being reported as missing. The obvious implementation is
to delete the expectation, and it is wrong for the same reason deleting a rule is wrong: findings
already raised cite the declaration that produced them, and a citation to a deleted row explains
nothing.

So retirement is an **end-date on the ``active`` range**, and the row stays. The rule's
``f.active @> :scope_period_start`` clause does the rest — periods before the end date still match
the declaration, periods after it do not.

Two asymmetric failure modes, both tested here:

* **Retiring too broadly** silently erases history. A steward investigating a September gap finds
  the expectation gone and cannot tell whether the feed was late or was never expected.
* **Retiring ineffectively** means a decommissioned feed reports missing every month forever, and
  the queue fills with findings nobody can act on. That is how a steward learns to ignore the queue.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import SourcePeriodScope, run_rules
from dq.rules.registry import retire_feed_expectation
from dq.seed.generator import MISSING_FEED_SOURCE, PERIODS, TARGET_PERIOD, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

#: The period the feed is absent for and the expectation still covers. Findings raised here must
#: survive retirement.
BEFORE_RETIREMENT = TARGET_PERIOD

#: A period after the end date. The feed is equally absent; no finding should be raised.
AFTER_RETIREMENT = (date(2026, 10, 1), date(2026, 11, 1))

#: The end date itself — the first day *not* covered, since the range is half-open.
RETIREMENT_DATE = AFTER_RETIREMENT[0]

_FEED_FINDINGS = text("""
SELECT f.subject_key, f.scope_key
FROM   finding f
JOIN   rule_version rv ON rv.rule_version_id = f.rule_version_id
JOIN   rule r          ON r.rule_id          = rv.rule_id
WHERE  r.rule_key = 'FEED-LATE-MISSING'
""")


@pytest.fixture(scope="module")
def retired(
    settings: Settings, seeded: SeedResult, registered: list[str], findings_reader: Connection
) -> Iterator[dict[str, Any]]:
    """Raise a finding while active, then end-date the expectation.

    Restored on teardown, because the missing-feed expectation is part of the seeded world that
    ``test_missing_feed.py`` and the golden set assert against.
    """
    source_id = seeded.source_ids[MISSING_FEED_SOURCE]

    # Evaluate the period the expectation still covers.
    run_rules(
        settings,
        SourcePeriodScope(MISSING_FEED_SOURCE, BEFORE_RETIREMENT[0], BEFORE_RETIREMENT[1]),
    )
    before = {(r.subject_key, r.scope_key) for r in findings_reader.execute(_FEED_FINDINGS)}

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        assert retire_feed_expectation(conn, source_id, RETIREMENT_DATE), (
            "retirement matched no expectation"
        )

    yield {"source_id": source_id, "findings_before": before}

    # Reopen the range so the rest of the suite sees the world it expects.
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        conn.execute(
            text(
                "UPDATE feed_expectation "
                "SET active = daterange(lower(active), NULL, '[)') "
                "WHERE source_system_id = :sid"
            ),
            {"sid": source_id},
        )


def test_a_finding_existed_before_retirement(retired: dict[str, Any]) -> None:
    """Without this the whole file passes against a feed rule that never fired."""
    assert retired["findings_before"], "no missing-feed finding was raised while active"


def test_the_expectation_is_end_dated_not_deleted(
    findings_reader: Connection, retired: dict[str, Any]
) -> None:
    """The row must still be there, with a bounded range.

    A deleted expectation would leave the findings above citing nothing.
    """
    row = findings_reader.execute(
        text(
            "SELECT lower(active) AS starts, upper(active) AS ends, cadence, delivery_window "
            "FROM feed_expectation WHERE source_system_id = :sid"
        ),
        {"sid": retired["source_id"]},
    ).one_or_none()

    assert row is not None, "retirement deleted the expectation"
    assert row.ends == RETIREMENT_DATE
    assert row.starts is not None, "retirement should move the upper bound only"
    assert row.cadence == "MONTHLY", "retirement must not rewrite what the declaration said"


def test_a_period_after_the_end_date_raises_nothing(
    settings: Settings, findings_reader: Connection, retired: dict[str, Any]
) -> None:
    """FR-021c. The feed is just as absent; it is simply no longer expected."""
    result = run_rules(
        settings,
        SourcePeriodScope(MISSING_FEED_SOURCE, AFTER_RETIREMENT[0], AFTER_RETIREMENT[1]),
    )
    assert result.status == "COMPLETED", result.status

    scope_key = f"sp:{MISSING_FEED_SOURCE}:{AFTER_RETIREMENT[0]:%Y-%m}"
    raised = {s for _, s in findings_reader.execute(_FEED_FINDINGS)}
    assert scope_key not in raised, (
        "a retired feed still reported missing — the queue would fill with findings nobody can act "
        "on, which is how a steward learns to ignore it"
    )


def test_findings_raised_before_retirement_are_retained(
    findings_reader: Connection, retired: dict[str, Any]
) -> None:
    """The half a delete would break."""
    after = {(r.subject_key, r.scope_key) for r in findings_reader.execute(_FEED_FINDINGS)}
    assert retired["findings_before"] <= after, (
        f"retirement removed historical findings: {sorted(retired['findings_before'] - after)}"
    )


def test_a_period_still_inside_the_range_is_unaffected(
    settings: Settings, findings_reader: Connection, retired: dict[str, Any]
) -> None:
    """The boundary works in both directions.

    A retirement that stopped *all* reporting rather than reporting after the end date would pass
    the test above and be badly wrong — re-running an earlier period is how a steward re-checks a
    historical gap.
    """
    earlier = PERIODS[1]
    run_rules(settings, SourcePeriodScope(MISSING_FEED_SOURCE, earlier[0], earlier[1]))

    covered = findings_reader.execute(
        text(
            "SELECT (active @> CAST(:d AS date)) AS still_covered "
            "FROM feed_expectation WHERE source_system_id = :sid"
        ),
        {"d": earlier[0], "sid": retired["source_id"]},
    ).scalar_one()
    assert covered is True, "a period before the end date should still be covered"


def test_retiring_an_unknown_source_reports_it(settings: Settings) -> None:
    """A silent no-op would make a typo in a decommissioning script look like success."""
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        assert retire_feed_expectation(conn, -1, RETIREMENT_DATE) is False


def test_author_cannot_rewrite_what_the_declaration_said(
    settings: Settings, retired: dict[str, Any]
) -> None:
    """The grant is column-scoped to ``active`` (migration 0009).

    Widening it to a table-level UPDATE would let the cadence or delivery window be edited under
    findings already judged against them — the same mistake as a mutable rule version.
    """
    from sqlalchemy.exc import ProgrammingError

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        with pytest.raises(ProgrammingError) as excinfo:
            conn.execute(
                text("UPDATE feed_expectation SET cadence = 'DAILY' WHERE source_system_id = :s"),
                {"s": retired["source_id"]},
            )
        assert getattr(excinfo.value.orig, "sqlstate", None) == "42501", (
            f"refused, but not for lack of privilege: {excinfo.value.orig}"
        )


def test_delivery_window_is_equally_protected(settings: Settings, retired: dict[str, Any]) -> None:
    from sqlalchemy.exc import ProgrammingError

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        with pytest.raises(ProgrammingError):
            conn.execute(
                text(
                    "UPDATE feed_expectation SET delivery_window = :w WHERE source_system_id = :s"
                ),
                {"w": timedelta(days=99), "s": retired["source_id"]},
            )
