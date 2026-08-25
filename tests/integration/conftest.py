"""Fixtures for the tests that need a seeded world.

Session-scoped and built once: seeding crosses the network to Singapore several hundred times, and
rebuilding it per test would dominate the suite. Every test here reads; none mutates, because
``data_batch`` and its member tables reject UPDATE and DELETE by trigger anyway.
"""

from __future__ import annotations

import pytest

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.rules.definition import LIBRARY_DIR, load_library
from dq.rules.registry import register
from dq.seed.generator import SeedResult, seed

# `settings` and `findings_reader` live in the root tests/conftest.py, because the volume suite
# needs them too and a fixture defined here is invisible outside this directory.


@pytest.fixture(scope="session")
def seeded(settings: Settings) -> SeedResult:
    """Three periods of synthetic data with the deliberate defect set injected."""
    return seed(settings, with_defects=True)


@pytest.fixture(scope="session")
def registered(settings: Settings, seeded: SeedResult) -> list[str]:
    """Register the nine shipped rules. Returns their keys.

    Depends on ``seeded`` only for ordering — the registry knows nothing about data — so that a
    failure in seeding is not reported as a registration failure.
    """
    keys: list[str] = []
    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        for definition in load_library(LIBRARY_DIR):
            register(conn, definition)
            keys.append(definition.rule_key)
    return keys
