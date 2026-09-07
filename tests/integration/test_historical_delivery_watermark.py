"""Later alignment and feed deliveries stay outside a pinned historical world (T080)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.rules.definition import LIBRARY_DIR, load_library

ALIGNMENT_SOURCE = "T080_ALIGNMENT"
FEED_SOURCE = "T080_FEED"
PERIOD_START = date(2027, 1, 1)
PERIOD_END = date(2027, 2, 1)


@dataclass(frozen=True, slots=True)
class HistoricalDeliveryWorld:
    subject_batch_id: int
    covered_txn_id: int
    outside_effective_txn_id: int
    feed_source_id: int
    watermark_before: int
    watermark_after: int


def _predicate(rule_key: str) -> str:
    return next(
        rule.predicate_sql for rule in load_library(LIBRARY_DIR) if rule.rule_key == rule_key
    )


def _insert_source(conn: Connection, code: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO source_system (code, name) VALUES (:code, :name) "
                "RETURNING source_system_id"
            ),
            {"code": code, "name": f"T080 test source {code}"},
        ).scalar_one()
    )


def _insert_batch(
    conn: Connection,
    *,
    source_id: int,
    arrival: datetime,
    period_start: date,
    period_end: date,
    record_count: int,
) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO data_batch "
                "(source_system_id, arrival_ts, business_period, record_count) "
                "VALUES (:source_id, :arrival, daterange(:period_start, :period_end, '[)'), "
                ":record_count) RETURNING batch_id"
            ),
            {
                "source_id": source_id,
                "arrival": arrival,
                "period_start": period_start,
                "period_end": period_end,
                "record_count": record_count,
            },
        ).scalar_one()
    )


@pytest.fixture(scope="module")
def historical_delivery_world(settings: Settings) -> HistoricalDeliveryWorld:
    """Commit two later deliveries under dedicated sources so other seeded scopes stay unchanged."""
    with db.connect(settings, Role.INGEST) as conn:
        alignment_source_id = _insert_source(conn, ALIGNMENT_SOURCE)
        feed_source_id = _insert_source(conn, FEED_SOURCE)
        subject_batch_id = _insert_batch(
            conn,
            source_id=alignment_source_id,
            arrival=datetime(2027, 1, 2, 4, tzinfo=UTC),
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            record_count=2,
        )
        covered_txn_id = int(
            conn.execute(
                text(
                    "INSERT INTO sales_transaction "
                    "(batch_id, source_system_id, source_txn_key, product_key, hcp_key, "
                    "territory_code, txn_date, quantity, uom) "
                    "VALUES (:batch_id, :source_id, 'T080-COVERED', 'PRODUCT-1', "
                    "'HCP-COVERED', 'TERR-1', :txn_date, 1, 'EA') RETURNING txn_id"
                ),
                {
                    "batch_id": subject_batch_id,
                    "source_id": alignment_source_id,
                    "txn_date": date(2027, 1, 15),
                },
            ).scalar_one()
        )
        outside_effective_txn_id = int(
            conn.execute(
                text(
                    "INSERT INTO sales_transaction "
                    "(batch_id, source_system_id, source_txn_key, product_key, hcp_key, "
                    "territory_code, txn_date, quantity, uom) "
                    "VALUES (:batch_id, :source_id, 'T080-OUTSIDE', 'PRODUCT-1', "
                    "'HCP-OUTSIDE', 'TERR-1', :txn_date, 1, 'EA') RETURNING txn_id"
                ),
                {
                    "batch_id": subject_batch_id,
                    "source_id": alignment_source_id,
                    "txn_date": date(2027, 1, 15),
                },
            ).scalar_one()
        )
        watermark_before = int(
            conn.execute(text("SELECT max(batch_id) FROM data_batch")).scalar_one()
        )

    with db.connect(settings, Role.AUTHOR) as conn:
        conn.execute(
            text(
                "INSERT INTO feed_expectation "
                "(source_system_id, cadence, delivery_window, active) "
                "VALUES (:source_id, 'MONTHLY', :window, daterange(:active_from, NULL, '[)'))"
            ),
            {
                "source_id": feed_source_id,
                "window": timedelta(days=5),
                "active_from": PERIOD_START,
            },
        )

    with db.connect(settings, Role.INGEST) as conn:
        alignment_batch_id = _insert_batch(
            conn,
            source_id=alignment_source_id,
            arrival=datetime(2027, 2, 2, 4, tzinfo=UTC),
            period_start=PERIOD_END,
            period_end=date(2027, 3, 1),
            record_count=2,
        )
        conn.execute(
            text(
                "INSERT INTO territory_alignment "
                "(batch_id, source_system_id, hcp_source_key, territory_code, effective) "
                "VALUES "
                "(:batch_id, :source_id, 'HCP-COVERED', 'TERR-1', "
                " daterange(:period_start, :period_end, '[)')), "
                "(:batch_id, :source_id, 'HCP-OUTSIDE', 'TERR-1', "
                " daterange(:period_end, :later_end, '[)'))"
            ),
            {
                "batch_id": alignment_batch_id,
                "source_id": alignment_source_id,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
                "later_end": date(2027, 3, 1),
            },
        )
        _insert_batch(
            conn,
            source_id=feed_source_id,
            # This row is inserted after the watermark is recorded, even though the source reports
            # an on-time arrival. Delivery visibility is determined by batch_id, not timestamp.
            arrival=datetime(2027, 1, 3, 4, tzinfo=UTC),
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            record_count=0,
        )
        watermark_after = int(
            conn.execute(text("SELECT max(batch_id) FROM data_batch")).scalar_one()
        )

    return HistoricalDeliveryWorld(
        subject_batch_id=subject_batch_id,
        covered_txn_id=covered_txn_id,
        outside_effective_txn_id=outside_effective_txn_id,
        feed_source_id=feed_source_id,
        watermark_before=watermark_before,
        watermark_after=watermark_after,
    )


def _alignment_findings(
    conn: Connection, world: HistoricalDeliveryWorld, watermark: int
) -> set[int]:
    rows = conn.execute(
        text(_predicate("SALES-NO-ALIGNMENT")),
        {"batch_id": world.subject_batch_id, "reference_watermark": watermark},
    )
    return {int(row.subject_key) for row in rows}


def _feed_findings(conn: Connection, world: HistoricalDeliveryWorld, watermark: int) -> list[str]:
    rows = conn.execute(
        text(_predicate("FEED-LATE-MISSING")),
        {
            "scope_source_system_id": world.feed_source_id,
            "scope_period_start": PERIOD_START,
            "scope_period_end": PERIOD_END,
            "reference_watermark": watermark,
        },
    )
    return [str(row.subject_key) for row in rows]


def test_historical_alignment_evaluation_excludes_later_delivery(
    findings_reader: Connection, historical_delivery_world: HistoricalDeliveryWorld
) -> None:
    world = historical_delivery_world
    historical = _alignment_findings(findings_reader, world, world.watermark_before)
    fresh = _alignment_findings(findings_reader, world, world.watermark_after)

    assert world.covered_txn_id in historical
    assert world.covered_txn_id not in fresh
    assert world.outside_effective_txn_id in historical
    assert world.outside_effective_txn_id in fresh, (
        "delivery-time visibility must not replace the alignment's business-effective-date check"
    )


def test_historical_feed_evaluation_excludes_later_delivery(
    findings_reader: Connection, historical_delivery_world: HistoricalDeliveryWorld
) -> None:
    world = historical_delivery_world
    historical = _feed_findings(findings_reader, world, world.watermark_before)
    fresh = _feed_findings(findings_reader, world, world.watermark_after)

    assert historical == [f"{FEED_SOURCE}:{PERIOD_START}"]
    assert fresh == []
