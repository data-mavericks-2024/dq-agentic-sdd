"""A new rule family produces findings with no engine change (T055a, SC-007).

Two halves, and **the second is the one that matters**.

The first half registers a rule family absent from the shipped library and asserts it produces
findings. That alone is weak evidence: it would pass just as well if someone had added a branch to
the runner to make this specific rule work.

The second half is the guard. It reads the engine's own source and asserts it contains **no rule
key and no commercial table name** — that the engine cannot be special-casing any rule, because it
has never heard of one. A checksum over the directory would be the obvious alternative and a bad
one: it fails on every legitimate refactor, so it gets updated reflexively until it means nothing.
Asserting *what must not appear* survives refactoring and still catches the thing worth catching.

SC-007 is what makes the rule registry a product surface rather than a configuration file. If adding
a rule needs an engine change, then a data engineer cannot add one, and every new check becomes a
release.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, run_rules
from dq.rules.definition import LIBRARY_DIR, RuleDefinition, load_library
from dq.rules.registry import register, set_active
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

ENGINE_DIR = Path(__file__).resolve().parents[2] / "src" / "dq" / "engine"

RULE_KEY = "TEST-HCO-AFFILIATION"

#: A genuinely new family: HCP records with no institutional affiliation. Nothing in the shipped
#: library checks this, and nothing in the engine has any reason to know it exists.
PREDICATE = """
SELECT h.hcp_id::text            AS subject_key,
       h.source_key              AS offending_value,
       '<no affiliation>'::text  AS observed_value,
       'an hco_source_key'::text AS expected_value
FROM   hcp h
WHERE  h.batch_id    =  :batch_id
  AND  h.valid_from <=  :as_of_date
  AND  h.batch_id   <=  :reference_watermark
  AND  NOT h.is_deleted
  AND  h.hco_source_key IS NULL
"""

DEFINITION = RuleDefinition.model_validate(
    {
        "rule_key": RULE_KEY,
        "domain": "HCP",
        "dimension": "completeness",
        "severity": "LOW",
        "owning_function": "Master Data Management",
        "subject_type": "record",
        "predicate_sql": PREDICATE,
        "parameters": {},
    }
)

#: Commercial tables the engine has no business naming. `data_batch` and `source_system` are
#: excluded: scope resolution legitimately reads both to find a batch's period and a source's id.
DOMAIN_TABLES = (
    "hcp",
    "hco",
    "product",
    "territory_alignment",
    "sales_transaction",
)


def _engine_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(ENGINE_DIR.rglob("*.py"))}


# ---------------------------------------------------------------------------
# The guard. Runs first because it needs no database and is the real assertion.
# ---------------------------------------------------------------------------


def test_the_engine_directory_exists_and_has_source() -> None:
    """Guards the two tests below from passing against an empty glob."""
    sources = _engine_sources()
    assert sources, f"no Python files found under {ENGINE_DIR}"
    assert "runner.py" in sources


def test_the_engine_names_no_rule_key() -> None:
    """If the engine special-cased a rule, that rule's key would appear in its source.

    Checked against the whole shipped library plus the family this test invents, so it catches both
    "someone hard-coded HCP-NPI-FORMAT" and "someone hard-coded the rule this test adds".
    """
    keys = [d.rule_key for d in load_library(LIBRARY_DIR)] + [RULE_KEY]
    offenders = [
        f"{name} mentions {key}"
        for name, source in _engine_sources().items()
        for key in keys
        if key in source
    ]
    assert not offenders, (
        "the engine references a specific rule, so adding a rule is not purely a registry "
        "operation:\n  " + "\n  ".join(offenders)
    )


def test_the_engine_names_no_commercial_table() -> None:
    """The engine reads `finding`, `rule_run` and scope metadata. It never reads domain data.

    A domain table name in engine source means the engine is doing part of a rule's job, which is
    the same failure as a hard-coded rule key wearing different clothes.
    """
    import re

    offenders = [
        f"{name} mentions {table}"
        for name, source in _engine_sources().items()
        for table in DOMAIN_TABLES
        if re.search(rf"\b{table}\b", source)
    ]
    assert not offenders, "the engine references commercial tables directly:\n  " + "\n  ".join(
        offenders
    )


# ---------------------------------------------------------------------------
# The demonstration.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def new_family(
    settings: Settings, seeded: SeedResult, registered: list[str]
) -> Iterator[dict[str, Any]]:
    """Register a rule family that does not exist in the library, and run it.

    Deactivated on teardown so a later full run does not see it — SC-001's set equality in
    ``test_golden_findings.py`` is asserted against the nine shipped families only.
    """
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        registration = register(conn, DEFINITION)

    result = run_rules(settings, BatchScope(batch.batch_id), rule_keys=[RULE_KEY])

    yield {"registration": registration, "result": result, "batch_id": batch.batch_id}

    with db.connect(settings, Role.AUTHOR) as conn:
        db.pin_session(conn, settings.schema_prefix)
        set_active(conn, RULE_KEY, False)


def test_the_family_is_genuinely_new(new_family: dict[str, Any]) -> None:
    """It must not already be in the library, or this proves nothing about extensibility."""
    assert RULE_KEY not in {d.rule_key for d in load_library(LIBRARY_DIR)}
    assert new_family["registration"].outcome == "created"


def test_the_new_rule_was_evaluated(new_family: dict[str, Any]) -> None:
    result = new_family["result"]
    assert result.status == "COMPLETED", result.status
    assert len(result.outcomes) == 1
    assert result.outcomes[0].rule_key == RULE_KEY
    assert result.outcomes[0].outcome == "EVALUATED"


def test_the_new_rule_produced_findings(
    findings_reader: Connection, new_family: dict[str, Any]
) -> None:
    """SC-007. Registering a row was the entire change."""
    count = findings_reader.execute(
        text("SELECT count(*) FROM finding WHERE rule_version_id = :v"),
        {"v": new_family["registration"].rule_version_id},
    ).scalar_one()
    assert count > 0, "the new family registered and ran but detected nothing"


def test_the_new_findings_carry_the_declared_severity(
    findings_reader: Connection, new_family: dict[str, Any]
) -> None:
    """Severity is snapshotted from the version, not looked up at read time.

    Which is why a later severity change cannot rewrite how a past finding was triaged.
    """
    severities = (
        findings_reader.execute(
            text("SELECT DISTINCT severity FROM finding WHERE rule_version_id = :v"),
            {"v": new_family["registration"].rule_version_id},
        )
        .scalars()
        .all()
    )
    assert severities == ["LOW"]
