"""VOL-DEVIATION's grain is `(product_key, territory_code)`, not `territory_code` alone (T084,
FR-022, spec Edge Cases: "Volume deviation on a new product").

Read-only against the golden dataset's own findings — this module creates no new rule or finding
row, so unlike `test_summary_version_dedup.py` it carries no risk to `test_golden_findings.py`'s
whole-table equality regardless of file ordering. `test_golden_findings.py`'s own fixture already
runs the scope this reads from; running it again here is a deliberate, harmless idempotent no-op
(FR-011), not a race — it makes this module correct standing alone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Settings
from dq.engine.runner import BatchScope, run_rules
from dq.seed.defects import (
    NEW_PRODUCT_NO_BASELINE_KEY,
    REALIGNED_PRODUCT_INDEX,
    REALIGNED_TERRITORY_INDEX,
)
from dq.seed.generator import PRODUCTS_PER_SOURCE, TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")


@pytest.fixture(scope="module")
def evaluated_batch(settings: Settings, seeded: SeedResult, registered: list[str]) -> SeedResult:
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    result = run_rules(settings, BatchScope(batch.batch_id))
    assert result.status != "FAILED", result.status
    return seeded


def _territory_code(source_id: int, index: int) -> str:
    return f"T{source_id}-{index:02d}"


def _product_key(source_id: int, index: int) -> str:
    return f"PRD-{source_id}-{index:03d}"


def _vol_deviation_subjects(findings_reader: Connection) -> set[str]:
    rows = findings_reader.execute(
        text(
            "SELECT f.subject_key FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = 'VOL-DEVIATION'"
        )
    ).all()
    return {row.subject_key for row in rows}


def test_the_realigned_product_fires_with_a_compound_subject_key(
    findings_reader: Connection, evaluated_batch: SeedResult
) -> None:
    sid = evaluated_batch.source_ids[TARGET_SOURCE]
    expected_subject = (
        f"{_territory_code(sid, REALIGNED_TERRITORY_INDEX)}:"
        f"{_product_key(sid, REALIGNED_PRODUCT_INDEX)}"
    )

    assert expected_subject in _vol_deviation_subjects(findings_reader)


def test_other_products_sharing_the_territory_do_not_fire(
    findings_reader: Connection, evaluated_batch: SeedResult
) -> None:
    """The territory-only grain this replaces would have reported one deviation for the whole
    territory. The corrected grain must show a deviation for exactly the one product that grew —
    proving the other five, which share the territory but received none of the extra volume, are
    correctly silent rather than merely untested."""
    sid = evaluated_batch.source_ids[TARGET_SOURCE]
    subjects = _vol_deviation_subjects(findings_reader)

    other_products = [i for i in range(PRODUCTS_PER_SOURCE) if i != REALIGNED_PRODUCT_INDEX]
    assert other_products, "test is vacuous if there is only one product"
    for product_index in other_products:
        subject = (
            f"{_territory_code(sid, REALIGNED_TERRITORY_INDEX)}:{_product_key(sid, product_index)}"
        )
        assert subject not in subjects, f"{subject} should have no baseline change, but fired"


def test_a_new_product_with_no_prior_period_does_not_fire(
    findings_reader: Connection, evaluated_batch: SeedResult
) -> None:
    """Spec Edge Cases, stated exactly: a product with no prior period to compare against must not
    produce a deviation, no matter how large its current-period volume is.
    `_inject_volume_deviation_no_baseline`'s transaction carries qty=10,000 specifically so a
    regression to a `LEFT JOIN` with a zero-fallback would produce an unmissable, not a subtle,
    false positive."""
    subjects = _vol_deviation_subjects(findings_reader)

    assert not any(NEW_PRODUCT_NO_BASELINE_KEY in subject for subject in subjects)


def test_the_same_transaction_is_still_a_genuine_orphan_reference(
    findings_reader: Connection, evaluated_batch: SeedResult
) -> None:
    """Spec Edge Cases: "two rules disagree in effect... both findings stand." VOL-DEVIATION is
    silent on this transaction; SALES-ORPHAN-REF is not, and should not be — the product genuinely
    does not exist in master data, independent of whether its volume looks unusual."""
    count = findings_reader.execute(
        text(
            "SELECT count(*) FROM finding f "
            "JOIN rule_version rv ON rv.rule_version_id = f.rule_version_id "
            "JOIN rule r ON r.rule_id = rv.rule_id "
            "WHERE r.rule_key = 'SALES-ORPHAN-REF' "
            "  AND f.offending_value = :val"
        ),
        {"val": f"product:{NEW_PRODUCT_NO_BASELINE_KEY}"},
    ).scalar_one()

    assert count == 1
