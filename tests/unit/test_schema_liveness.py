"""Unit coverage for advisory-lock test-session liveness (T077)."""

from __future__ import annotations

from dq.db.test_isolation import advisory_lock_key


def test_advisory_lock_key_is_stable_and_prefix_specific() -> None:
    prefix = "test_20260824143022_a1b2c3_"

    assert advisory_lock_key(prefix) == advisory_lock_key(prefix)
    assert advisory_lock_key(prefix) != advisory_lock_key("test_20260824143022_d4e5f6_")


def test_advisory_lock_key_fits_signed_bigint() -> None:
    key = advisory_lock_key("test_20260824143022_a1b2c3_")

    assert -(2**63) <= key < 2**63
