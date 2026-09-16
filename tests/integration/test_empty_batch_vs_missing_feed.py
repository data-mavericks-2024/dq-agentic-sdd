"""An empty batch is distinguishable from a missing feed (T087, US1/AC3, spec Edge Cases).

Spec Edge Cases, stated exactly: "A batch is empty. A feed arrives containing zero records. This
must be distinguishable from 'the feed did not arrive' — they have different causes and different
remediations."

Uses a source system of its own, declared and delivered to only by this module, rather than
reusing `TARGET_SOURCE` or `MISSING_FEED_SOURCE`. Both of those already have a settled answer to
"did this period's feed arrive?" baked into the golden set's own expectations; proving an *empty*
delivery is still a delivery needs a period where nothing else has already answered that question.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, SourcePeriodScope, run_rules
from dq.seed.generator import _insert_batch

pytestmark = pytest.mark.usefixtures("registered")

SOURCE_CODE = "EMPTY-BATCH-TEST-SRC"
PERIOD_START = date(2027, 1, 1)
PERIOD_END = date(2027, 2, 1)


@pytest.fixture(scope="module")
def empty_batch(settings: Settings, registered: list[str]) -> int:
    """Declare a feed, then deliver it — with zero records. Not the same fact as never declaring
    a delivery at all, which is what this module exists to keep separate."""
    with db.connect(settings, Role.INGEST) as conn:
        db.pin_session(conn, settings.schema_prefix)
        source_id = conn.execute(
            text(
                "INSERT INTO source_system (code, name) VALUES (:c, :n) RETURNING source_system_id"
            ),
            {"c": SOURCE_CODE, "n": "Empty Batch Test Source"},
        ).scalar_one()
        batch_id = _insert_batch(
            conn,
            source_id,
            (PERIOD_START, PERIOD_END),
            datetime(2027, 1, 3, 4, 0, tzinfo=UTC),
            count=0,
        )

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        conn.execute(
            text(
                "INSERT INTO feed_expectation (source_system_id, cadence, delivery_window, active) "
                "VALUES (:s, 'MONTHLY', :w, daterange(:active_from, NULL, '[)'))"
            ),
            {"s": source_id, "w": timedelta(days=5), "active_from": PERIOD_START},
        )

    return batch_id


def test_the_empty_batch_exists_as_a_queryable_row(
    findings_reader: Connection, empty_batch: int
) -> None:
    """The structural fact that makes it a different case from a missing feed: there is a row."""
    row = findings_reader.execute(
        text("SELECT record_count, arrival_ts FROM data_batch WHERE batch_id = :b"),
        {"b": empty_batch},
    ).one()

    assert row.record_count == 0
    assert row.arrival_ts is not None


def test_a_batch_scoped_run_over_it_completes_clean(settings: Settings, empty_batch: int) -> None:
    result = run_rules(settings, BatchScope(empty_batch))

    assert result.status == "COMPLETED", result.status
    assert result.finding_count == 0


def test_the_period_is_not_reported_as_a_missing_feed(settings: Settings, empty_batch: int) -> None:
    """The point of the whole module: an empty delivery satisfies "did the feed arrive?" — only
    the *absence* of any batch does not."""
    result = run_rules(settings, SourcePeriodScope(SOURCE_CODE, PERIOD_START, PERIOD_END))

    assert result.status == "COMPLETED", result.status
    assert result.finding_count == 0


def test_a_genuinely_missing_feed_has_no_batch_row_at_all(
    findings_reader: Connection, empty_batch: int
) -> None:
    """The contrasting fact, made explicit rather than left implicit: the feed this module
    declares and delivers to (with zero records) has a `data_batch` row. `MISSING_FEED_SOURCE`'s
    absent period — proven to produce a FEED-LATE-MISSING finding in `test_missing_feed.py` — has
    none at all. Different causes, different remediations, different queryable evidence."""
    from dq.seed.generator import MISSING_FEED_SOURCE, TARGET_PERIOD

    count = findings_reader.execute(
        text(
            "SELECT count(*) FROM data_batch b "
            "JOIN source_system s ON s.source_system_id = b.source_system_id "
            "WHERE s.code = :code AND b.business_period && daterange(:start, :end, '[)')"
        ),
        {"code": MISSING_FEED_SOURCE, "start": TARGET_PERIOD[0], "end": TARGET_PERIOD[1]},
    ).scalar_one()

    assert count == 0
