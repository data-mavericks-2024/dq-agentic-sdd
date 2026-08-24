"""The ``commercial`` schema — curated commercial data.

Two modelling decisions run through everything here, and both are deliberate inversions of what a
normal transactional schema would do.

**Master data is an append-only version chain, not a current-state table** (research.md D9). Each
row is one version of one natural key ``(source_system_id, source_key)``, stamped with the
``valid_from`` date it applies from. Nothing is ever updated or deleted. There is no ``valid_to``:
the version in effect on a date is the one with the greatest ``valid_from`` not after it. Storing
an end date would require updating the previous row, and a mutable master table reintroduces
exactly the reproducibility problem this design exists to remove.

**Constraints protect referential truth; rules detect data-quality defects. Where they conflict,
the rule wins.** ``hcp.npi`` is nullable and unvalidated, ``sales_transaction`` stores master
references as text rather than foreign keys, and ``territory_alignment`` carries no exclusion
constraint — because a constraint that rejects the defect at insert makes the defect permanently
undetectable, and SC-001 requires every injected defect family to be detected.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import DATERANGE

from dq.db.schemas import Schema
from dq.domain import metadata

_S: Final[str] = Schema.COMMERCIAL.value

#: Master tables are append-only version chains. Named here because the immutability trigger
#: (T019) and the predicate validator's as-of check (T025 rule 7) both key off this list.
MASTER_TABLES: Final[tuple[str, ...]] = ("hcp", "hco", "product", "territory")

#: Every table whose rows belong to a batch and are therefore immutable once written (FR-003b).
#: Revision 1 protected only the batch header, which left the contents mutable.
BATCH_MEMBER_TABLES: Final[tuple[str, ...]] = (
    "hcp",
    "hco",
    "product",
    "territory",
    "territory_alignment",
    "sales_transaction",
)


def _source_system_fk() -> Column[int]:
    return Column(
        "source_system_id",
        SmallInteger,
        ForeignKey(f"{_S}.source_system.source_system_id"),
        nullable=False,
    )


def _batch_fk() -> Column[int]:
    return Column("batch_id", BigInteger, ForeignKey(f"{_S}.data_batch.batch_id"), nullable=False)


# ---------------------------------------------------------------------------
# T014 — source_system, data_batch
# ---------------------------------------------------------------------------

source_system = Table(
    "source_system",
    metadata,
    Column("source_system_id", SmallInteger, Identity(always=False), primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    schema=_S,
)


data_batch = Table(
    "data_batch",
    metadata,
    Column("batch_id", BigInteger, Identity(always=False), primary_key=True),
    _source_system_fk(),
    Column("arrival_ts", DateTime(timezone=True), nullable=False),
    # The period the data describes, not when it arrived. FR-021b's late/missing logic compares
    # this to arrival_ts, which is why the engine pins TimeZone (research.md D10).
    Column("business_period", DATERANGE, nullable=False),
    Column("record_count", Integer, nullable=False),
    Column("sealed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Zero is legal and means "the feed arrived and was empty" — a different fact from "the feed
    # never arrived", and FR-021 must be able to tell them apart.
    CheckConstraint("record_count >= 0", name="record_count_non_negative"),
    UniqueConstraint("source_system_id", "arrival_ts"),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T015 — the four master tables, as append-only version chains
# ---------------------------------------------------------------------------

hcp = Table(
    "hcp",
    metadata,
    # Surrogate key for this *version*, not for the practitioner.
    Column("hcp_id", BigInteger, Identity(always=False), primary_key=True),
    _source_system_fk(),
    Column("source_key", Text, nullable=False),
    Column("valid_from", Date, nullable=False),
    _batch_fk(),
    Column("is_deleted", Boolean, nullable=False, server_default=text("false")),
    # Nullable and unconstrained on purpose. Rejecting a malformed NPI at insert would prevent the
    # engine from ever detecting one, and FR-015 exists to detect them.
    Column("npi", Text, nullable=True),
    Column("first_name", Text, nullable=True),
    Column("last_name", Text, nullable=True),
    Column("postal_code", Text, nullable=True),
    Column("licence_state", Text, nullable=True),
    # Affiliation carried by natural key, not FK — an orphan affiliation is a defect to detect.
    Column("hco_source_key", Text, nullable=True),
    # Without this, "the version in effect on date D" has no single answer and the as-of idiom
    # stops being deterministic.
    UniqueConstraint("source_system_id", "source_key", "valid_from"),
    schema=_S,
)


hco = Table(
    "hco",
    metadata,
    Column("hco_id", BigInteger, Identity(always=False), primary_key=True),
    _source_system_fk(),
    Column("source_key", Text, nullable=False),
    Column("valid_from", Date, nullable=False),
    _batch_fk(),
    Column("is_deleted", Boolean, nullable=False, server_default=text("false")),
    Column("name", Text, nullable=True),
    Column("postal_code", Text, nullable=True),
    UniqueConstraint("source_system_id", "source_key", "valid_from"),
    schema=_S,
)


product = Table(
    "product",
    metadata,
    Column("product_id", BigInteger, Identity(always=False), primary_key=True),
    _source_system_fk(),
    Column("source_key", Text, nullable=False),
    Column("valid_from", Date, nullable=False),
    _batch_fk(),
    Column("is_deleted", Boolean, nullable=False, server_default=text("false")),
    Column("name", Text, nullable=True),
    # Canonical unit of measure. FR-020 compares this to sales_transaction.uom.
    Column("uom", Text, nullable=True),
    Column("status", Text, nullable=True),
    Column("retired_on", Date, nullable=True),
    CheckConstraint("status IS NULL OR status IN ('ACTIVE','RETIRED')", name="status_domain"),
    UniqueConstraint("source_system_id", "source_key", "valid_from"),
    schema=_S,
)


territory = Table(
    "territory",
    metadata,
    Column("territory_id", BigInteger, Identity(always=False), primary_key=True),
    _source_system_fk(),
    Column("source_key", Text, nullable=False),
    Column("valid_from", Date, nullable=False),
    _batch_fk(),
    Column("is_deleted", Boolean, nullable=False, server_default=text("false")),
    Column("code", Text, nullable=True),
    Column("name", Text, nullable=True),
    UniqueConstraint("source_system_id", "source_key", "valid_from"),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T016 — territory_alignment. No exclusion constraint, deliberately.
# ---------------------------------------------------------------------------

territory_alignment = Table(
    "territory_alignment",
    metadata,
    Column("alignment_id", BigInteger, Identity(always=False), primary_key=True),
    _batch_fk(),
    _source_system_fk(),
    Column("hcp_source_key", Text, nullable=False),
    Column("territory_code", Text, nullable=False),
    # Half-open `[from, to)`. The only convention under which consecutive periods neither overlap
    # by a day nor leave a phantom one-day gap — which is exactly the defect family FR-019 must
    # detect genuinely rather than spuriously (research.md D7).
    Column("effective", DATERANGE, nullable=False),
    schema=_S,
)
# Revision 1 carried `EXCLUDE USING gist (hcp_id WITH =, effective WITH &&)`, which made an
# overlapping alignment uninsertable — and therefore made FR-019's overlap defect impossible to
# inject, so SC-001 could not cover it. Overlap is a data-quality defect: the rule detects it and
# no constraint prevents it landing.


# ---------------------------------------------------------------------------
# T017 — sales_transaction
# ---------------------------------------------------------------------------

sales_transaction = Table(
    "sales_transaction",
    metadata,
    Column("txn_id", BigInteger, Identity(always=False), primary_key=True),
    _batch_fk(),
    _source_system_fk(),
    Column("source_txn_key", Text, nullable=False),
    # Text, not foreign keys. A foreign key would reject the orphaned transaction at insert,
    # making FR-017 permanently undetectable.
    Column("product_key", Text, nullable=False),
    Column("hcp_key", Text, nullable=False),
    Column("territory_code", Text, nullable=False),
    Column("txn_date", Date, nullable=False),
    # Exact, not floating point: `sum` and `avg` over numeric are order-independent, so FR-022's
    # period-over-period comparison cannot change answer with the query plan.
    Column("quantity", Numeric(18, 4), nullable=False),
    Column("uom", Text, nullable=False),
    schema=_S,
)


# ---------------------------------------------------------------------------
# T018 — indexes
# ---------------------------------------------------------------------------
#
# `COLLATE "C"` on the FR-016b composite-match columns is explicit and load-bearing. A managed-
# platform glibc or ICU upgrade changes text sort order under a locale-dependent collation, leaving
# existing indexes inconsistent — so an index scan and a sequential scan of the same predicate can
# return different rows. `"C"` is byte-ordered, immune to that, and faster. Under a
# non-deterministic ICU collation, text `=` would also become case- and accent-insensitive,
# silently redefining what FR-016b matches without a new rule version.

#: The four columns FR-016b matches on, each byte-ordered. Asserted by name in
#: tests/integration/test_schema_indexes.py.
COMPOSITE_MATCH_INDEX: Final[str] = "ix_hcp_composite_match"

_indexes = [
    # As-of resolution: the `ORDER BY valid_from DESC LIMIT 1` idiom reads straight down this.
    Index("ix_hcp_as_of", hcp.c.source_system_id, hcp.c.source_key, hcp.c.valid_from.desc()),
    Index("ix_hco_as_of", hco.c.source_system_id, hco.c.source_key, hco.c.valid_from.desc()),
    Index(
        "ix_product_as_of",
        product.c.source_system_id,
        product.c.source_key,
        product.c.valid_from.desc(),
    ),
    Index(
        "ix_territory_as_of",
        territory.c.source_system_id,
        territory.c.source_key,
        territory.c.valid_from.desc(),
    ),
    # FR-016a — HCP records sharing an NPI under distinct surrogate keys.
    Index("ix_hcp_npi", hcp.c.npi, postgresql_where=hcp.c.npi.isnot(None)),
    # FR-016b — composite match. `left(first_name, 1)` is IMMUTABLE, so it is indexable.
    Index(
        COMPOSITE_MATCH_INDEX,
        text('last_name COLLATE "C"'),
        text('left(first_name, 1) COLLATE "C"'),
        text('postal_code COLLATE "C"'),
        text('licence_state COLLATE "C"'),
        _table=hcp,
    ),
    # FR-018 — range containment for "was this HCP aligned to this territory on this date".
    # GiST over a smallint and a text alongside a daterange needs btree_gist, created in
    # migration 0002.
    Index(
        "ix_territory_alignment_lookup",
        territory_alignment.c.source_system_id,
        territory_alignment.c.hcp_source_key,
        territory_alignment.c.effective,
        postgresql_using="gist",
    ),
    Index("ix_sales_transaction_txn_date", sales_transaction.c.txn_date),
    Index(
        "ix_sales_transaction_product",
        sales_transaction.c.source_system_id,
        sales_transaction.c.product_key,
    ),
    Index(
        "ix_sales_transaction_hcp",
        sales_transaction.c.source_system_id,
        sales_transaction.c.hcp_key,
    ),
    Index("ix_sales_transaction_batch", sales_transaction.c.batch_id),
]

#: Index names T018's test asserts exist. Kept as data so the test cannot drift from the schema.
EXPECTED_INDEXES: Final[tuple[str, ...]] = tuple(ix.name for ix in _indexes if ix.name)
