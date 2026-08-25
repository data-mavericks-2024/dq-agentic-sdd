"""Synthetic commercial data (T049) and the feed expectation catalogue (T049a).

Three consecutive monthly periods across two source systems, with master data delivered as **version
chains** — the same natural key redelivered each period with a later ``valid_from``. That shape is
the point: a generator that produced current-state rows would never exercise as-of resolution, and
SC-010's historical re-run would pass for the wrong reason.

**Composite-key uniqueness among non-defect HCPs is guaranteed, not hoped for.** FR-016b matches on
last name, first initial, postal code, and licence state. If two generated HCPs collided on all four
by chance, the duplicate rule would fire on a record nobody injected, SC-001's set equality would
fail, and the failure would look like an engine bug. Every non-defect HCP gets a globally unique
postal code, which makes the collision impossible rather than unlikely.

Writes commercial data as **``dq_ingest``** and the feed catalogue as **``dq_author``**. Two roles,
because a feed expectation is authored configuration of the same kind as a rule, and ``dq_ingest``
is deliberately blind to quality metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db

# ---------------------------------------------------------------------------
# Shape of the generated world
# ---------------------------------------------------------------------------

SOURCES: tuple[tuple[str, str], ...] = (
    ("VEEVA", "Veeva CRM"),
    ("IQVIA_DDD", "IQVIA Direct Distributor Data"),
)

#: Three consecutive months. The first is baseline, the second is the volume comparison period,
#: and the third is where defects are injected.
PERIODS: tuple[tuple[date, date], ...] = (
    (date(2026, 7, 1), date(2026, 8, 1)),
    (date(2026, 8, 1), date(2026, 9, 1)),
    (date(2026, 9, 1), date(2026, 10, 1)),
)

#: The source and period defects are injected into, and the scope the golden test evaluates.
TARGET_SOURCE = "VEEVA"
TARGET_PERIOD = PERIODS[2]

#: The feed deliberately absent for the last period, so FR-021d has something to detect.
MISSING_FEED_SOURCE = "IQVIA_DDD"

HCPS_PER_SOURCE = 24
PRODUCTS_PER_SOURCE = 6
TERRITORIES_PER_SOURCE = 4
HCOS_PER_SOURCE = 5

#: Transactions per territory per batch. Large enough that the injected defect rows cannot move a
#: territory's period-over-period volume anywhere near VOL-DEVIATION's 20% threshold — otherwise
#: injecting a referential-integrity defect would raise a volume finding nobody asked for.
TXNS_PER_TERRITORY = 30
BASELINE_QTY = 100

_SURNAMES = (  # noqa: SIM905 — one string per line is unreadable at 24 names
    "Abernathy Bellweather Castellano Drummond Eastwood Fairweather Goldsmith Harrington "
    "Ivanovic Jankowski Kirkpatrick Lindqvist Moreau Nakamura Oyelaran Pemberton Quintero "
    "Rasmussen Stavros Thornbury Ueda Vasquez Whitfield Xiong"
).split()

_GIVEN = (  # noqa: SIM905 — as above
    "Amara Bennet Cecile Dorian Elise Fabian Greta Hollis Imani Jules Katya Lorne "
    "Mireille Nadir Odile Priya Quill Rosalind Sasha Tobias Ursula Verity Wilhelmina Xavier"
).split()

_STATES = ("CA", "NY", "TX", "IL", "MA", "WA")
_UOMS = ("EA", "ML", "MG")


@dataclass(frozen=True, slots=True)
class BatchRef:
    source_code: str
    source_system_id: int
    period_start: date
    period_end: date
    batch_id: int

    @property
    def period_label(self) -> str:
        return f"{self.period_start:%Y-%m}"


@dataclass
class SeedResult:
    """Everything a test needs to assert against, without re-querying."""

    source_ids: dict[str, int] = field(default_factory=dict)
    batches: list[BatchRef] = field(default_factory=list)
    #: ``(rule_key, subject_key)`` pairs the injected defects are expected to produce.
    expected: set[tuple[str, str]] = field(default_factory=set)
    #: Human-readable note per expectation, so a set-equality failure can explain itself.
    notes: dict[tuple[str, str], str] = field(default_factory=dict)

    def batch(self, source_code: str, period_start: date) -> BatchRef:
        for b in self.batches:
            if b.source_code == source_code and b.period_start == period_start:
                return b
        raise KeyError(f"no batch for {source_code} {period_start:%Y-%m}")

    def expect(self, rule_key: str, subject_key: str, note: str) -> None:
        self.expected.add((rule_key, subject_key))
        self.notes[(rule_key, subject_key)] = note


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def _insert_batch(
    conn: Connection, source_id: int, period: tuple[date, date], arrival: datetime, count: int
) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO data_batch (source_system_id, arrival_ts, business_period, "
                "record_count) VALUES (:sid, :ts, "
                "daterange(:period_start, :period_end, '[)'), :n) RETURNING batch_id"
            ),
            {
                "sid": source_id,
                "ts": arrival,
                "period_start": period[0],
                "period_end": period[1],
                "n": count,
            },
        ).scalar_one()
    )


def _hcp_row(
    source_id: int, batch_id: int, index: int, valid_from: date, *, global_offset: int
) -> dict[str, object]:
    """One non-defect HCP version.

    ``postal_code`` is globally unique, which is what makes an accidental FR-016b composite
    collision impossible rather than merely unlikely.
    """
    unique = global_offset + index
    return {
        "sid": source_id,
        "key": f"HCP-{source_id}-{index:04d}",
        "vf": valid_from,
        "bid": batch_id,
        "npi": f"{1000000000 + unique:010d}",
        "first": _GIVEN[index % len(_GIVEN)],
        "last": _SURNAMES[index % len(_SURNAMES)],
        "postal": f"{10000 + unique:05d}",
        "state": _STATES[index % len(_STATES)],
        "hco": f"HCO-{source_id}-{index % HCOS_PER_SOURCE:03d}",
    }


_INSERT_HCP = text(
    "INSERT INTO hcp (source_system_id, source_key, valid_from, batch_id, npi, first_name, "
    "last_name, postal_code, licence_state, hco_source_key) "
    "VALUES (:sid, :key, :vf, :bid, :npi, :first, :last, :postal, :state, :hco)"
)

_INSERT_PRODUCT = text(
    "INSERT INTO product (source_system_id, source_key, valid_from, batch_id, name, uom, status) "
    "VALUES (:sid, :key, :vf, :bid, :name, :uom, 'ACTIVE')"
)

_INSERT_TERRITORY = text(
    "INSERT INTO territory (source_system_id, source_key, valid_from, batch_id, code, name) "
    "VALUES (:sid, :key, :vf, :bid, :code, :name)"
)

_INSERT_HCO = text(
    "INSERT INTO hco (source_system_id, source_key, valid_from, batch_id, name, postal_code) "
    "VALUES (:sid, :key, :vf, :bid, :name, :postal)"
)

_INSERT_ALIGNMENT = text(
    "INSERT INTO territory_alignment (batch_id, source_system_id, hcp_source_key, territory_code, "
    "effective) VALUES (:bid, :sid, :hcp, :terr, daterange(:eff_from, :eff_to, '[)'))"
)

_INSERT_TXN = text(
    "INSERT INTO sales_transaction (batch_id, source_system_id, source_txn_key, product_key, "
    "hcp_key, territory_code, txn_date, quantity, uom) "
    "VALUES (:bid, :sid, :tkey, :pkey, :hkey, :terr, :d, :qty, :uom)"
)


def _territory_code(source_id: int, index: int) -> str:
    return f"T{source_id}-{index:02d}"


def _product_key(source_id: int, index: int) -> str:
    return f"PRD-{source_id}-{index:03d}"


def _product_uom(index: int) -> str:
    return _UOMS[index % len(_UOMS)]


# ---------------------------------------------------------------------------
# Base world
# ---------------------------------------------------------------------------


def _seed_sources(conn: Connection) -> dict[str, int]:
    ids: dict[str, int] = {}
    for code, name in SOURCES:
        existing = conn.execute(
            text("SELECT source_system_id FROM source_system WHERE code = :c"), {"c": code}
        ).scalar_one_or_none()
        if existing is None:
            existing = conn.execute(
                text(
                    "INSERT INTO source_system (code, name) VALUES (:c, :n) "
                    "RETURNING source_system_id"
                ),
                {"c": code, "n": name},
            ).scalar_one()
        ids[code] = int(existing)
    return ids


def _seed_master(
    conn: Connection, source_id: int, batch_id: int, valid_from: date, offset: int
) -> None:
    """One full delivery of every master domain, as a new version of each natural key."""
    conn.execute(
        _INSERT_HCP,
        [
            _hcp_row(source_id, batch_id, i, valid_from, global_offset=offset)
            for i in range(HCPS_PER_SOURCE)
        ],
    )
    conn.execute(
        _INSERT_PRODUCT,
        [
            {
                "sid": source_id,
                "key": _product_key(source_id, i),
                "vf": valid_from,
                "bid": batch_id,
                "name": f"Product {source_id}-{i}",
                "uom": _product_uom(i),
            }
            for i in range(PRODUCTS_PER_SOURCE)
        ],
    )
    conn.execute(
        _INSERT_TERRITORY,
        [
            {
                "sid": source_id,
                "key": _territory_code(source_id, i),
                "vf": valid_from,
                "bid": batch_id,
                "code": _territory_code(source_id, i),
                "name": f"Territory {source_id}-{i}",
            }
            for i in range(TERRITORIES_PER_SOURCE)
        ],
    )
    conn.execute(
        _INSERT_HCO,
        [
            {
                "sid": source_id,
                "key": f"HCO-{source_id}-{i:03d}",
                "vf": valid_from,
                "bid": batch_id,
                "name": f"Institution {source_id}-{i}",
                "postal": f"{90000 + i:05d}",
            }
            for i in range(HCOS_PER_SOURCE)
        ],
    )


def _seed_alignments(conn: Connection, source_id: int, batch_id: int) -> None:
    """One clean, gapless, non-overlapping assignment per HCP for the whole span.

    Delivered once, in the first batch. Every defect in the FR-019 family is injected against this
    baseline, so an overlap or a gap in the results is one that was put there on purpose.
    """
    span_from, span_to = PERIODS[0][0], PERIODS[-1][1]
    conn.execute(
        _INSERT_ALIGNMENT,
        [
            {
                "bid": batch_id,
                "sid": source_id,
                "hcp": f"HCP-{source_id}-{i:04d}",
                "terr": _territory_code(source_id, i % TERRITORIES_PER_SOURCE),
                "eff_from": span_from,
                "eff_to": span_to,
            }
            for i in range(HCPS_PER_SOURCE)
        ],
    )


#: Master rows delivered per batch, across all four domains.
MASTER_ROWS_PER_BATCH = (
    HCPS_PER_SOURCE + PRODUCTS_PER_SOURCE + TERRITORIES_PER_SOURCE + HCOS_PER_SOURCE
)


def _transaction_rows(source_id: int, period: tuple[date, date]) -> list[dict[str, object]]:
    """Clean transactions: every reference resolves, every alignment covers, every UoM agrees.

    Built before the batch exists, because ``data_batch`` is immutable by trigger — the row count
    cannot be patched in afterwards, so it has to be known before the header is written. ``batch_id``
    is filled in by the caller once the header exists.
    """
    rows: list[dict[str, object]] = []
    for t in range(TERRITORIES_PER_SOURCE):
        for n in range(TXNS_PER_TERRITORY):
            hcp_index = (t + n * TERRITORIES_PER_SOURCE) % HCPS_PER_SOURCE
            # Only HCPs aligned to this territory, or SALES-NO-ALIGNMENT fires on clean data and
            # SC-001's set equality fails against defects nobody injected.
            if hcp_index % TERRITORIES_PER_SOURCE != t:
                continue
            product_index = n % PRODUCTS_PER_SOURCE
            rows.append(
                {
                    "sid": source_id,
                    "tkey": f"TXN-{source_id}-{period[0]:%Y%m}-{t}-{n}",
                    "pkey": _product_key(source_id, product_index),
                    "hkey": f"HCP-{source_id}-{hcp_index:04d}",
                    "terr": _territory_code(source_id, t),
                    "d": period[0] + timedelta(days=n % 27),
                    "qty": BASELINE_QTY,
                    "uom": _product_uom(product_index),
                }
            )
    return rows


def _seed_feed_expectations(settings: Settings, source_ids: dict[str, int]) -> None:
    """Declare what should arrive, for both sources (T049a, FR-021a).

    One feed delivers on cadence for all three periods; the other is deliberately absent for the
    last, which is what gives FR-021d something to detect. Written as ``dq_author``.
    """
    with db.connect(settings, Role.AUTHOR) as conn:
        for code in (TARGET_SOURCE, MISSING_FEED_SOURCE):
            already = conn.execute(
                text("SELECT 1 FROM feed_expectation WHERE source_system_id = :s"),
                {"s": source_ids[code]},
            ).scalar()
            if already:
                continue
            conn.execute(
                text(
                    "INSERT INTO feed_expectation (source_system_id, cadence, delivery_window, "
                    "active) VALUES (:s, 'MONTHLY', :w, daterange(:active_from, NULL, '[)'))"
                ),
                {
                    "s": source_ids[code],
                    "w": timedelta(days=5),
                    # Open-ended: retiring a feed is an end-date, which is what FR-021c tests.
                    "active_from": PERIODS[0][0],
                },
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def seed(settings: Settings, *, with_defects: bool = True) -> SeedResult:
    """Generate the world. Returns what was created and what is expected to fail."""
    from dq.seed.defects import inject

    result = SeedResult()

    with db.connect(settings, Role.INGEST) as conn:
        db.pin_session(conn, settings.schema_prefix)
        result.source_ids = _seed_sources(conn)

        arrival_base = datetime(2026, 7, 3, 4, 0, tzinfo=UTC)
        offset = 0
        for source_index, (code, _) in enumerate(SOURCES):
            source_id = result.source_ids[code]
            for period_index, period in enumerate(PERIODS):
                # The absent delivery. Nothing is inserted at all — not an empty batch, which is a
                # different fact and one FR-021 must be able to tell apart.
                if code == MISSING_FEED_SOURCE and period == PERIODS[-1]:
                    continue

                arrival = arrival_base + timedelta(days=31 * period_index, hours=source_index)
                txn_rows = _transaction_rows(source_id, period)
                alignment_rows = HCPS_PER_SOURCE if period_index == 0 else 0
                record_count = len(txn_rows) + MASTER_ROWS_PER_BATCH + alignment_rows

                batch_id = _insert_batch(conn, source_id, period, arrival, record_count)
                _seed_master(conn, source_id, batch_id, period[0], offset)
                if period_index == 0:
                    _seed_alignments(conn, source_id, batch_id)
                for row in txn_rows:
                    row["bid"] = batch_id
                if txn_rows:
                    conn.execute(_INSERT_TXN, txn_rows)

                result.batches.append(BatchRef(code, source_id, period[0], period[1], batch_id))
                offset += HCPS_PER_SOURCE

        if with_defects:
            inject(conn, result)

    _seed_feed_expectations(settings, result.source_ids)

    if with_defects:
        # Aggregate expectations do not depend on a connection, so they are recorded once the
        # world exists rather than during injection.
        from dq.seed.defects import expect_aggregates

        expect_aggregates(result)

    return result
