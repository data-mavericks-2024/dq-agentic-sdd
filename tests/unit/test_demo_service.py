from __future__ import annotations

from datetime import date

import pytest

from dq.demo.service import NightlyBatch, NightlySource, build_scope_keys, parse_period


def test_parse_period_uses_half_open_month() -> None:
    assert parse_period("2026-09") == (date(2026, 9, 1), date(2026, 10, 1))
    assert parse_period("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))


@pytest.mark.parametrize("value", ["", "2026", "2026-9", "September 2026", "2026-13"])
def test_parse_period_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        parse_period(value)


def test_scope_keys_cover_delivered_batches_and_missing_sources() -> None:
    batches = [
        NightlyBatch(41, "VEEVA", "Veeva CRM", date(2026, 9, 1), date(2026, 10, 1), 900),
    ]
    sources = [
        NightlySource(1, "VEEVA", "Veeva CRM"),
        NightlySource(2, "IQVIA_DDD", "IQVIA DDD"),
    ]

    assert build_scope_keys("2026-09", batches, sources) == (
        "b:41",
        "sp:IQVIA_DDD:2026-09",
        "sp:VEEVA:2026-09",
    )
