"""SC-008 — a full rule run at target scale completes inside ten minutes (T052).

**Run this early, not last.** It is the check that catches an architecturally wrong design, and its
entire value lies in catching that before three more user stories are built on top. If set-based
evaluation cannot hit the budget on free-tier compute, the options are indexing work, a lower stated
target, or paid compute — all far cheaper to face now than at Feature 5.

Data is generated **server-side with ``generate_series``**. Inserting a million rows from Windows to
Singapore would take far longer than the budget being measured and would tell you nothing about rule
performance (research.md D8).

Storage was sized before this was written: 1M transactions and 100K HCPs with their indexes project
to ~229 MB against a 500 MB ceiling, of which indexes are more than half. Marked ``volume`` and
excluded from the default suite.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, run_rules
from dq.rules.definition import LIBRARY_DIR, load_library
from dq.rules.registry import register

pytestmark = pytest.mark.volume

#: SC-008's budget.
BUDGET_SECONDS = 600

TXN_ROWS = 1_000_000
HCP_ROWS = 100_000
PRODUCT_ROWS = 5_000
TERRITORY_ROWS = 5_000

_AS_OF = "2026-09-30"


def _generate(conn: Connection) -> int:
    """Build the dataset inside the database and return the batch id under evaluation."""
    # Select-then-insert rather than `ON CONFLICT DO UPDATE`: the upsert form needs UPDATE
    # privilege, and `dq_ingest` holds only SELECT and INSERT on commercial. That refusal is the
    # grant matrix working, not an obstacle to route around.
    source_id = conn.execute(
        text("SELECT source_system_id FROM source_system WHERE code = 'VOLUME'")
    ).scalar_one_or_none()
    if source_id is None:
        source_id = conn.execute(
            text(
                "INSERT INTO source_system (code, name) VALUES ('VOLUME', 'Volume test') "
                "RETURNING source_system_id"
            )
        ).scalar_one()

    batch_id = conn.execute(
        text(
            "INSERT INTO data_batch (source_system_id, arrival_ts, business_period, record_count) "
            "VALUES (:s, now(), daterange('2026-09-01','2026-10-01','[)'), :n) RETURNING batch_id"
        ),
        {"s": source_id, "n": TXN_ROWS + HCP_ROWS},
    ).scalar_one()

    conn.execute(
        text("""
        INSERT INTO hcp (source_system_id, source_key, valid_from, batch_id, npi,
                         first_name, last_name, postal_code, licence_state)
        SELECT :s, 'VH-' || g, DATE '2026-09-01', :b,
               lpad(g::text, 10, '0'),
               'First' || g, 'Last' || g, lpad(mod(g, 99999)::text, 5, '0'), 'CA'
        FROM generate_series(1, :n) g
        """),
        {"s": source_id, "b": batch_id, "n": HCP_ROWS},
    )

    conn.execute(
        text("""
        INSERT INTO product (source_system_id, source_key, valid_from, batch_id, name, uom, status)
        SELECT :s, 'VP-' || g, DATE '2026-09-01', :b, 'Product ' || g, 'EA', 'ACTIVE'
        FROM generate_series(1, :n) g
        """),
        {"s": source_id, "b": batch_id, "n": PRODUCT_ROWS},
    )

    conn.execute(
        text("""
        INSERT INTO territory_alignment (batch_id, source_system_id, hcp_source_key,
                                         territory_code, effective)
        SELECT :b, :s, 'VH-' || g, 'VT-' || mod(g, :t),
               daterange(DATE '2026-01-01', DATE '2027-01-01', '[)')
        FROM generate_series(1, :n) g
        """),
        {"b": batch_id, "s": source_id, "t": TERRITORY_ROWS, "n": HCP_ROWS},
    )

    conn.execute(
        text("""
        INSERT INTO sales_transaction (batch_id, source_system_id, source_txn_key, product_key,
                                       hcp_key, territory_code, txn_date, quantity, uom)
        SELECT :b, :s, 'VT-' || g,
               'VP-' || (1 + mod(g, :p)),
               'VH-' || (1 + mod(g, :h)),
               'VT-' || mod(1 + mod(g, :h), :t),
               DATE '2026-09-01' + mod(g, 29),
               100.0, 'EA'
        FROM generate_series(1, :n) g
        """),
        {
            "b": batch_id,
            "s": source_id,
            "p": PRODUCT_ROWS,
            "h": HCP_ROWS,
            "t": TERRITORY_ROWS,
            "n": TXN_ROWS,
        },
    )

    conn.execute(text("ANALYZE"))
    return int(batch_id)


@pytest.fixture(scope="module")
def volume_batch(settings: Settings) -> Iterator[int]:
    """Generate, yield, and leave to the session teardown to drop with the schema."""
    with db.connect(settings, Role.INGEST) as conn:
        db.pin_session(conn, settings.schema_prefix)
        batch_id = _generate(conn)
    yield batch_id


@pytest.fixture(scope="module")
def volume_rules(settings: Settings) -> list[str]:
    keys = []
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        for definition in load_library(LIBRARY_DIR):
            register(conn, definition)
            keys.append(definition.rule_key)
    return keys


def test_full_rule_run_completes_within_budget(
    settings: Settings, volume_batch: int, volume_rules: list[str]
) -> None:
    """One statement per rule, not per record. That is what makes this possible at all."""
    started = time.monotonic()
    result = run_rules(settings, BatchScope(volume_batch))
    elapsed = time.monotonic() - started

    print(f"\nvolume run: {elapsed:.1f}s for {TXN_ROWS:,} transactions, {HCP_ROWS:,} HCPs")
    for outcome in result.outcomes:
        print(f"  {outcome.rule_key:<20} {outcome.outcome:<10} {outcome.finding_count:>8}")

    assert result.status != "FAILED", result.status
    assert elapsed < BUDGET_SECONDS, (
        f"SC-008 missed: {elapsed:.1f}s against a {BUDGET_SECONDS}s budget. "
        f"The options are indexing work, a lower stated target, or paid compute — this is the "
        f"check that exists to force that decision before more is built on top."
    )


def test_storage_stayed_within_the_free_tier(
    settings: Settings, volume_batch: int, findings_reader: Connection
) -> None:
    """The other half of R1. Compute and storage fail independently."""
    size_mb = findings_reader.execute(
        text("""
        SELECT round(sum(pg_total_relation_size(c.oid)) / 1024.0 / 1024.0, 1)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname LIKE :prefix AND c.relkind = 'r'
        """),
        {"prefix": f"{settings.schema_prefix}%"},
    ).scalar_one()

    print(f"\nvolume dataset: {size_mb} MB")
    assert float(size_mb) < 450, (
        f"{size_mb} MB leaves too little headroom under the 500 MB free-tier ceiling once "
        f"development schemas and an orphaned test schema are counted."
    )
