"""CMS NPI check-digit and synthetic-data guarantees (T078, FR-015)."""

from __future__ import annotations

from datetime import date

import pytest

from dq.seed.generator import _hcp_row, _npi_check_digit, _synthetic_npi


def _passes_cms_luhn(npi: str) -> bool:
    """Independent assertion helper using the full CMS ``80840`` prefix."""
    if len(npi) != 10 or not npi.isascii() or not npi.isdigit():
        return False
    digits = [int(digit) for digit in f"80840{npi}"]
    total = 0
    for position_from_right, digit in enumerate(reversed(digits)):
        value = digit * 2 if position_from_right % 2 else digit
        total += value // 10 + value % 10
    return total % 10 == 0


def test_official_cms_check_digit_vector() -> None:
    assert _npi_check_digit("123456789") == "3"
    assert _passes_cms_luhn("1234567893")
    assert not _passes_cms_luhn("1234567890")


@pytest.mark.parametrize("identifier", ["", "12345678", "1234567890", "12345678A"])
def test_check_digit_rejects_anything_except_nine_ascii_digits(identifier: str) -> None:
    with pytest.raises(ValueError, match="nine ASCII digits"):
        _npi_check_digit(identifier)


def test_synthetic_npi_is_deterministic_and_checksum_valid() -> None:
    first = _synthetic_npi(42)
    assert first == _synthetic_npi(42)
    assert _passes_cms_luhn(first)


@pytest.mark.parametrize("unique", [-1, 100_000_000])
def test_synthetic_npi_rejects_values_outside_its_reserved_range(unique: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 99999999"):
        _synthetic_npi(unique)


def test_generated_non_defect_hcps_have_valid_npis() -> None:
    for offset in (0, 1_000, 90_000_000):
        first = [_hcp_row(1, 1, i, date(2026, 9, 1), global_offset=offset) for i in range(24)]
        second = [_hcp_row(1, 1, i, date(2026, 9, 1), global_offset=offset) for i in range(24)]
        assert first == second
        for row in first:
            assert _passes_cms_luhn(str(row["npi"]))
