"""Deliberate defect injection, and the expected finding set (T050).

**The expected set is emitted as the defects are injected, never maintained by hand.** A
hand-written list is a second source of truth that drifts from the generator, and when it drifts the
symptom is SC-001 failing for a reason that has nothing to do with the engine. Here, injecting a
defect and declaring the finding it should produce are the same statement.

Two things make this harder than it looks, and both are handled explicitly below:

**Defects interact.** An orphaned transaction has no HCP, so it has no alignment either — and would
raise SALES-NO-ALIGNMENT as well as SALES-ORPHAN-REF. That second finding is real, but it belongs to
a defect nobody injected, and it would make set equality fail. Where an interaction is unwanted it is
neutralised at the source (the orphan keys get alignments); where it is genuine it is declared.

**One of these is not a defect at all.** ``VOL-DEVIATION`` fires on a legitimate territory
realignment. Constitution X requires the golden set to contain a scenario whose correct conclusion
is "the rule is wrong, not the data", and Feature 1 is where seed data exists — retrofitting it at
Feature 3 would mean seeding backwards from an agent's conclusion, which proves nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Connection, text

from dq.seed.generator import (
    _INSERT_ALIGNMENT,
    _INSERT_HCP,
    _INSERT_TXN,
    MISSING_FEED_SOURCE,
    PERIODS,
    TARGET_PERIOD,
    TARGET_SOURCE,
    _product_key,
    _product_uom,
    _territory_code,
)

if TYPE_CHECKING:
    from dq.seed.generator import SeedResult

#: The territory whose volume legitimately jumps. Chosen away from territory 0 so the realignment
#: cannot be confused with the transaction-level defects, which all land in territory 0.
REALIGNED_TERRITORY_INDEX = 2

#: How much the realigned territory grows. Comfortably past VOL-DEVIATION's 20% threshold, because
#: a scenario that only just trips the rule tests the arithmetic rather than the judgement.
REALIGNMENT_GROWTH = 0.35


def _insert_hcp_returning(conn: Connection, **row: object) -> int:
    return int(
        conn.execute(
            text(str(_INSERT_HCP) + " RETURNING hcp_id"),
            row,
        ).scalar_one()
    )


def _insert_txn_returning(conn: Connection, **row: object) -> int:
    return int(
        conn.execute(
            text(str(_INSERT_TXN) + " RETURNING txn_id"),
            row,
        ).scalar_one()
    )


def _insert_alignment_returning(conn: Connection, **row: object) -> int:
    return int(
        conn.execute(
            text(str(_INSERT_ALIGNMENT) + " RETURNING alignment_id"),
            row,
        ).scalar_one()
    )


def inject(conn: Connection, result: SeedResult) -> None:
    """Inject one defect family at a time into the target batch, recording what each should raise."""
    batch = result.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    sid = batch.source_system_id
    bid = batch.batch_id
    other_sid = result.source_ids[MISSING_FEED_SOURCE]
    valid_from = TARGET_PERIOD[0]

    _inject_npi_defects(conn, result, sid, bid, valid_from)
    _inject_duplicate_npi(conn, result, sid, other_sid, bid, valid_from)
    _inject_namesake_pair(conn, result, sid, other_sid, bid, valid_from)
    _inject_orphan_references(conn, result, sid, bid)
    _inject_missing_alignment(conn, result, sid, bid)
    _inject_uom_mismatch(conn, result, sid, bid)
    _inject_alignment_overlap_and_gap(conn, result, sid, bid)
    _inject_legitimate_realignment(conn, sid, bid)


# ---------------------------------------------------------------------------
# FR-015 — missing or malformed NPI
# ---------------------------------------------------------------------------


def _inject_npi_defects(
    conn: Connection, result: SeedResult, sid: int, bid: int, valid_from: date
) -> None:
    """Two HCPs: one with no NPI at all, one with an NPI that is the wrong shape.

    Both carry a unique postal code and a unique surname, so neither can also trip either duplicate
    rule — the point of injecting one family at a time is lost if the records overlap.
    """
    cases = [
        (None, "absent NPI"),
        ("123", "malformed NPI — three digits, not ten"),
    ]
    for i, (npi, note) in enumerate(cases):
        hcp_id = _insert_hcp_returning(
            conn,
            sid=sid,
            key=f"HCP-DEFECT-NPI-{i}",
            vf=valid_from,
            bid=bid,
            npi=npi,
            first=f"Npidefect{i}",
            last=f"Npidefect{i}",
            postal=f"7{i:04d}",
            state="CA",
            hco=None,
        )
        result.expect("HCP-NPI-FORMAT", str(hcp_id), note)


# ---------------------------------------------------------------------------
# FR-016a — the same NPI under two natural keys
# ---------------------------------------------------------------------------


def _inject_duplicate_npi(
    conn: Connection, result: SeedResult, sid: int, other_sid: int, bid: int, valid_from: date
) -> None:
    """An NPI already held by an HCP in the *other* source system.

    Cross-source is the case the spec names as primary, and it is the one a batch-scoped duplicate
    check would be blind to. Only the record in the evaluated batch is a subject; its counterpart
    lives in an earlier batch of another feed and is not re-flagged.
    """
    partner_npi = conn.execute(
        text(
            "SELECT npi FROM hcp WHERE source_system_id = :s AND npi IS NOT NULL "
            "ORDER BY source_key, valid_from LIMIT 1"
        ),
        {"s": other_sid},
    ).scalar_one()

    hcp_id = _insert_hcp_returning(
        conn,
        sid=sid,
        key="HCP-DEFECT-DUPNPI",
        vf=valid_from,
        bid=bid,
        npi=partner_npi,
        first="Dupnpi",
        last="Dupnpi",
        postal="70100",
        state="NY",
        hco=None,
    )
    result.expect(
        "HCP-DUP-NPI", str(hcp_id), f"shares NPI {partner_npi} with an {MISSING_FEED_SOURCE} record"
    )


# ---------------------------------------------------------------------------
# FR-016b — the namesake pair the spec's edge cases require
# ---------------------------------------------------------------------------


def _inject_namesake_pair(
    conn: Connection, result: SeedResult, sid: int, other_sid: int, bid: int, valid_from: date
) -> None:
    """An HCP matching an existing record on surname, first initial, postal code, and licence state.

    **This is an expected finding, not a false positive.** Two genuine practitioners can share all
    four attributes, which is exactly why FR-016b is MEDIUM and why SC-001 asserts set *equality*
    against a declared expectation rather than promising zero findings on clean data. A steward is
    meant to look at this and decide; the system is not meant to be certain.
    """
    partner = conn.execute(
        text(
            "SELECT last_name, first_name, postal_code, licence_state FROM hcp "
            "WHERE source_system_id = :s AND postal_code IS NOT NULL "
            "ORDER BY source_key, valid_from LIMIT 1"
        ),
        {"s": other_sid},
    ).one()

    hcp_id = _insert_hcp_returning(
        conn,
        sid=sid,
        key="HCP-DEFECT-NAMESAKE",
        vf=valid_from,
        bid=bid,
        # Unique NPI, so this record trips the composite rule and nothing else.
        npi="9999999001",
        first=partner.first_name,
        last=partner.last_name,
        postal=partner.postal_code,
        state=partner.licence_state,
        hco=None,
    )
    result.expect(
        "HCP-DUP-COMPOSITE",
        str(hcp_id),
        f"namesake of an {MISSING_FEED_SOURCE} record: {partner.last_name}, "
        f"{partner.first_name[0]}, {partner.postal_code}, {partner.licence_state}",
    )


# ---------------------------------------------------------------------------
# FR-017 — orphaned references
# ---------------------------------------------------------------------------


def _inject_orphan_references(conn: Connection, result: SeedResult, sid: int, bid: int) -> None:
    """One transaction naming an unknown HCP, one naming an unknown product.

    **Each orphan key is given a territory alignment first.** Without it the unknown HCP would also
    have no alignment, so SALES-NO-ALIGNMENT would fire on a transaction injected to test a
    different rule — a real finding, but one that belongs to nothing, and enough on its own to fail
    SC-001's set equality.
    """
    territory = _territory_code(sid, 0)
    span_from, span_to = PERIODS[0][0], PERIODS[-1][1]
    txn_date = TARGET_PERIOD[0] + timedelta(days=3)

    for orphan_hcp in ("HCP-GHOST-0001",):
        conn.execute(
            _INSERT_ALIGNMENT,
            {
                "bid": bid,
                "sid": sid,
                "hcp": orphan_hcp,
                "terr": territory,
                "eff_from": span_from,
                "eff_to": span_to,
            },
        )

    txn_id = _insert_txn_returning(
        conn,
        bid=bid,
        sid=sid,
        tkey="TXN-DEFECT-ORPHAN-HCP",
        pkey=_product_key(sid, 0),
        hkey="HCP-GHOST-0001",
        terr=territory,
        d=txn_date,
        qty=1,
        uom=_product_uom(0),
    )
    result.expect("SALES-ORPHAN-REF", str(txn_id), "references an HCP absent from master data")

    # The unknown *product* case needs no alignment work: its HCP is real and aligned, and the
    # UoM rule's LATERAL join finds no product version, so it contributes no row.
    txn_id = _insert_txn_returning(
        conn,
        bid=bid,
        sid=sid,
        tkey="TXN-DEFECT-ORPHAN-PRODUCT",
        pkey="PRD-GHOST-0001",
        hkey=f"HCP-{sid}-0000",
        terr=_territory_code(sid, 0),
        d=txn_date,
        qty=1,
        uom="EA",
    )
    result.expect("SALES-ORPHAN-REF", str(txn_id), "references a product absent from master data")


# ---------------------------------------------------------------------------
# FR-018 — a sale credited to a territory with no alignment
# ---------------------------------------------------------------------------


def _inject_missing_alignment(conn: Connection, result: SeedResult, sid: int, bid: int) -> None:
    """A real HCP, a real product, matching UoM — credited to a territory it was never aligned to."""
    txn_id = _insert_txn_returning(
        conn,
        bid=bid,
        sid=sid,
        tkey="TXN-DEFECT-NOALIGN",
        pkey=_product_key(sid, 1),
        hkey=f"HCP-{sid}-0000",
        # HCP 0000 is aligned to territory 0; crediting it to territory 1 is the defect.
        terr=_territory_code(sid, 1),
        d=TARGET_PERIOD[0] + timedelta(days=4),
        qty=1,
        uom=_product_uom(1),
    )
    result.expect(
        "SALES-NO-ALIGNMENT", str(txn_id), "credited to a territory the HCP was never aligned to"
    )


# ---------------------------------------------------------------------------
# FR-020 — unit-of-measure disagreement
# ---------------------------------------------------------------------------


def _inject_uom_mismatch(conn: Connection, result: SeedResult, sid: int, bid: int) -> None:
    """A transaction whose UoM contradicts the product master version in effect."""
    product_index = 0
    master_uom = _product_uom(product_index)
    wrong_uom = "ML" if master_uom != "ML" else "MG"

    txn_id = _insert_txn_returning(
        conn,
        bid=bid,
        sid=sid,
        tkey="TXN-DEFECT-UOM",
        pkey=_product_key(sid, product_index),
        hkey=f"HCP-{sid}-0000",
        terr=_territory_code(sid, 0),
        d=TARGET_PERIOD[0] + timedelta(days=5),
        qty=1,
        uom=wrong_uom,
    )
    result.expect(
        "SALES-UOM-MISMATCH", str(txn_id), f"transaction says {wrong_uom}, master says {master_uom}"
    )


# ---------------------------------------------------------------------------
# FR-019 — overlapping and gapped alignment
# ---------------------------------------------------------------------------


def _inject_alignment_overlap_and_gap(
    conn: Connection, result: SeedResult, sid: int, bid: int
) -> None:
    """Two separate HCPs: one whose assignments overlap, one whose assignments leave a hole.

    Both are insertable only because ``territory_alignment`` carries no exclusion constraint. An
    earlier design had one, which made the overlap defect impossible to seed and therefore impossible
    for SC-001 to cover (research.md D7).

    Neither HCP has transactions, so the gap cannot also raise SALES-NO-ALIGNMENT.
    """
    # --- overlap: two assignments for one HCP that intersect ---------------
    overlap_hcp = "HCP-DEFECT-OVERLAP"
    first = _insert_alignment_returning(
        conn,
        bid=bid,
        sid=sid,
        hcp=overlap_hcp,
        terr=_territory_code(sid, 0),
        eff_from=date(2026, 7, 1),
        eff_to=date(2026, 9, 1),
    )
    second = _insert_alignment_returning(
        conn,
        bid=bid,
        sid=sid,
        hcp=overlap_hcp,
        terr=_territory_code(sid, 1),
        eff_from=date(2026, 8, 1),
        eff_to=date(2026, 10, 1),
    )
    # Both rows are in the evaluated batch and both overlap the other, so both are subjects.
    result.expect("ALIGN-OVERLAP-GAP", str(first), "overlaps the following assignment by one month")
    result.expect(
        "ALIGN-OVERLAP-GAP", str(second), "overlaps the preceding assignment by one month"
    )

    # --- gap: a month with no assignment at all ---------------------------
    gap_hcp = "HCP-DEFECT-GAP"
    early = _insert_alignment_returning(
        conn,
        bid=bid,
        sid=sid,
        hcp=gap_hcp,
        terr=_territory_code(sid, 0),
        eff_from=date(2026, 7, 1),
        eff_to=date(2026, 8, 1),
    )
    _insert_alignment_returning(
        conn,
        bid=bid,
        sid=sid,
        hcp=gap_hcp,
        terr=_territory_code(sid, 1),
        eff_from=date(2026, 9, 1),
        eff_to=date(2026, 10, 1),
    )
    # Only the *earlier* row is a subject: the gap is detected as "something starts after this one
    # ends, and nothing covers the join". The later row has nothing after it, so it is clean.
    result.expect("ALIGN-OVERLAP-GAP", str(early), "August is left uncovered by any assignment")


# ---------------------------------------------------------------------------
# The scenario where the rule is wrong and the data is right
# ---------------------------------------------------------------------------


def _inject_legitimate_realignment(conn: Connection, sid: int, bid: int) -> None:
    """Grow one territory's volume by 35% — legitimately.

    A field expansion moved practitioners into this territory. Every transaction is correct, every
    reference resolves, and VOL-DEVIATION fires anyway because its 20% threshold has never accounted
    for planned realignment.

    The correct resolution is a new rule version, not a correction. Constitution X requires this
    scenario to exist in the golden set before any agent is built against it — an agent that can only
    ever confirm a problem erodes steward trust faster than one that is occasionally wrong.
    """
    from dq.seed.generator import BASELINE_QTY, HCPS_PER_SOURCE, TXNS_PER_TERRITORY

    territory = _territory_code(sid, REALIGNED_TERRITORY_INDEX)
    aligned = [
        i
        for i in range(HCPS_PER_SOURCE)
        if i % 4 == REALIGNED_TERRITORY_INDEX  # TERRITORIES_PER_SOURCE
    ]
    extra = max(1, int(TXNS_PER_TERRITORY * REALIGNMENT_GROWTH))

    rows = [
        {
            "bid": bid,
            "sid": sid,
            "tkey": f"TXN-REALIGN-{n}",
            "pkey": _product_key(sid, n % 6),
            "hkey": f"HCP-{sid}-{aligned[n % len(aligned)]:04d}",
            "terr": territory,
            "d": TARGET_PERIOD[0] + timedelta(days=10 + (n % 15)),
            "qty": BASELINE_QTY,
            "uom": _product_uom(n % 6),
        }
        for n in range(extra)
    ]
    conn.execute(_INSERT_TXN, rows)


def expect_aggregates(result: SeedResult) -> None:
    """Declare the two aggregate findings, which have no row to attach to.

    Recorded after the world exists rather than during injection, because neither is caused by a row
    that was inserted — one is caused by a batch that was *not*, and the other by a threshold that
    was never revised.
    """
    period_label = f"{TARGET_PERIOD[0]:%Y-%m-%d}"

    # FR-021d — the feed that never arrived. There is no batch, no row, and nothing to point at
    # except the (source, period) pair itself. This is the finding an earlier design could not
    # record at all.
    result.expect(
        "FEED-LATE-MISSING",
        f"{MISSING_FEED_SOURCE}:{period_label}",
        "declared monthly feed delivered nothing for this period",
    )

    # FR-022 — the rule is wrong, not the data.
    result.expect(
        "VOL-DEVIATION",
        _territory_code(result.source_ids[TARGET_SOURCE], REALIGNED_TERRITORY_INDEX),
        "legitimate territory realignment; threshold has not been revised since it was set",
    )
