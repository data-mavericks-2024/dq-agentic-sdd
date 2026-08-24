"""T007 — measure per-row storage and decide whether the SC-008 volume test fits the free tier.

    uv run python scripts/measure_storage.py [--rows 20000]

Creates a scratch schema, applies the *real* table and index definitions from ``dq.domain`` into
it, fills ``sales_transaction`` and ``hcp`` server-side with ``generate_series``, measures
``pg_total_relation_size`` (heap plus indexes plus TOAST), extrapolates to the SC-008 target of 1M
transactions and 100K HCP versions, and drops the scratch schema.

Measured rather than estimated because the answer decides whether T052 is buildable at all, and
because index overhead on this schema is not a small correction: ``sales_transaction`` carries four
indexes and ``hcp`` three, one of which is a four-column functional index.

Generated server-side for the same reason the volume test is: shipping a million rows from Windows
to Singapore would take far longer than the budget being measured.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import Connection, MetaData, create_engine, text

from dq.config.settings import as_psycopg_url, load_dotenv, require
from dq.db.schemas import Schema, physical, translate_map
from dq.domain import commercial, metadata

#: SC-008's stated scale (plan.md § Scale/Scope).
TARGET_TXN = 1_000_000
TARGET_HCP = 100_000

#: Supabase free tier.
FREE_TIER_BYTES = 500 * 1024 * 1024

#: The tables the volume test actually fills, plus the two they reference.
MEASURED = ("sales_transaction", "hcp")
REQUIRED = ("source_system", "data_batch", "hcp", "sales_transaction")


def _fill(conn: Connection, schema: str, rows: int) -> None:
    """Populate the scratch tables with realistically-shaped data.

    Shape matters more than realism: `hcp` composite-match columns must be varied or the
    four-column index compresses to nothing and understates its own cost, and `npi` must be
    present or its partial index measures empty.
    """
    conn.execute(
        text(f"INSERT INTO \"{schema}\".source_system (code, name) VALUES ('MEASURE', 'measure')")
    )
    conn.execute(
        text(
            f'INSERT INTO "{schema}".data_batch '
            "(source_system_id, arrival_ts, business_period, record_count) "
            "SELECT source_system_id, now(), "
            "daterange(DATE '2026-01-01', DATE '2026-02-01', '[)'), :n "
            f'FROM "{schema}".source_system'
        ),
        {"n": rows},
    )

    # `mod(a, b)` rather than `a % b`: these statements carry bind parameters, so SQLAlchemy
    # escapes every literal `%` for psycopg's pyformat paramstyle and getting the doubling right by
    # hand is needless risk. `mod` is also IMMUTABLE, which keeps this consistent with what a rule
    # predicate would be allowed to use.
    conn.execute(
        text(
            f'INSERT INTO "{schema}".hcp '
            "(source_system_id, source_key, valid_from, batch_id, is_deleted, npi, "
            " first_name, last_name, postal_code, licence_state, hco_source_key) "
            "SELECT s.source_system_id, "
            "       'HCP-' || g::text, "
            "       DATE '2026-01-01', "
            "       b.batch_id, false, "
            "       lpad((1000000000 + g)::text, 10, '0'), "
            "       'First' || mod(g, 5000)::text, "
            "       'Last' || mod(g, 9000)::text, "
            "       lpad(mod(g * 7, 99999)::text, 5, '0'), "
            "       (ARRAY['CA','NY','TX','FL','IL','PA','OH','GA'])[1 + mod(g, 8)], "
            "       'HCO-' || mod(g, 500)::text "
            f'FROM generate_series(1, :n) g, "{schema}".source_system s, "{schema}".data_batch b'
        ),
        {"n": rows},
    )

    conn.execute(
        text(
            f'INSERT INTO "{schema}".sales_transaction '
            "(batch_id, source_system_id, source_txn_key, product_key, hcp_key, "
            " territory_code, txn_date, quantity, uom) "
            "SELECT b.batch_id, s.source_system_id, "
            "       'TXN-' || g::text, "
            "       'PRD-' || mod(g, 5000)::text, "
            "       'HCP-' || (mod(g, :n) + 1)::text, "
            "       'TER-' || mod(g, 5000)::text, "
            "       DATE '2026-01-01' + mod(g, 28), "
            "       (mod(g, 500) + 1)::numeric(18,4), "
            "       'EA' "
            f'FROM generate_series(1, :n) g, "{schema}".source_system s, "{schema}".data_batch b'
        ),
        {"n": rows},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20_000)
    args = parser.parse_args()

    load_dotenv()
    prefix = f"measure_{uuid.uuid4().hex[:6]}_"
    url = as_psycopg_url(require("SUPABASE_DB_STATEFUL_URL"))

    engine = create_engine(url, future=True, pool_pre_ping=True).execution_options(
        schema_translate_map=translate_map(prefix)
    )
    scratch = physical(Schema.COMMERCIAL, prefix)

    # Only the four tables the measurement needs, so the scratch schema stays small and the drop
    # is quick. Index definitions come along with their tables.
    subset = MetaData(naming_convention=metadata.naming_convention)
    for name in REQUIRED:
        metadata.tables[f"commercial.{name}"].to_metadata(subset)

    # Created as the connected user — the database owner — rather than as dq_migrate, which holds
    # no CREATE on the database (see the note in migration 0005). This schema is a throwaway, so
    # its ownership carries no meaning.
    print(f"scratch schema: {scratch}   rows: {args.rows:,}\n")
    results: dict[str, tuple[int, int]] = {}

    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{scratch}"'))
            subset.create_all(bind=conn, checkfirst=False)
            _fill(conn, scratch, args.rows)
            conn.execute(text(f'ANALYZE "{scratch}".hcp'))
            conn.execute(text(f'ANALYZE "{scratch}".sales_transaction'))

            for table in MEASURED:
                total = conn.execute(
                    text("SELECT pg_total_relation_size(:t)"), {"t": f"{scratch}.{table}"}
                ).scalar_one()
                heap = conn.execute(
                    text("SELECT pg_table_size(:t)"), {"t": f"{scratch}.{table}"}
                ).scalar_one()
                results[table] = (int(total), int(heap))

        targets = {"sales_transaction": TARGET_TXN, "hcp": TARGET_HCP}
        projected_total = 0
        print(f"{'table':<20} {'bytes/row':>10} {'heap':>10} {'index':>10} {'projected':>14}")
        print("-" * 70)
        for table, (total, heap) in results.items():
            per_row = total / args.rows
            index = total - heap
            projected = int(per_row * targets[table])
            projected_total += projected
            print(
                f"{table:<20} {per_row:>10.1f} {heap / args.rows:>10.1f} "
                f"{index / args.rows:>10.1f} {projected / 1024 / 1024:>11.1f} MB"
            )

        print("-" * 70)
        print(f"{'projected total':<20} {projected_total / 1024 / 1024:>48.1f} MB")
        print(f"{'free tier ceiling':<20} {FREE_TIER_BYTES / 1024 / 1024:>48.1f} MB")
        headroom = FREE_TIER_BYTES - projected_total
        print(f"{'headroom':<20} {headroom / 1024 / 1024:>48.1f} MB")
        print()
        if headroom < 0:
            print("VERDICT: the SC-008 volume test does NOT fit the free tier as designed.")
        elif headroom < 150 * 1024 * 1024:
            print("VERDICT: it fits, but with less than 150 MB spare for findings, WAL, and any")
            print("         orphaned test schema. Treat as tight — run the sweep before T052.")
        else:
            print("VERDICT: the SC-008 volume test fits the free tier with room to spare.")
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{scratch}" CASCADE'))
        engine.dispose()
        print(f"\nscratch schema {scratch} dropped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


_ = commercial  # imported for its side effect of registering tables on `metadata`
