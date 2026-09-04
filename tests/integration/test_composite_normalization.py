"""Equivalent HCP composite values are matched after deterministic normalization (T079)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.rules.definition import LIBRARY_DIR, load_definition

RULE_PATH = LIBRARY_DIR / "hcp_dup_composite.yaml"
AS_OF_DATE = date(2026, 9, 30)

_STRIP_SPACE_AND_PUNCTUATION = "'[[:space:][:punct:]]+', '', 'g'"
_NORMALIZED_LAST_NAME = (
    f'regexp_replace(lower(last_name COLLATE "C"), {_STRIP_SPACE_AND_PUNCTUATION}) COLLATE "C"'
)
_NORMALIZED_FIRST_INITIAL = (
    f'left(regexp_replace(lower(first_name COLLATE "C"), '
    f"{_STRIP_SPACE_AND_PUNCTUATION}), 1) " + 'COLLATE "C"'
)
_NORMALIZED_POSTAL_CODE = (
    f'left(regexp_replace(lower(postal_code COLLATE "C"), '
    f"{_STRIP_SPACE_AND_PUNCTUATION}), 5) " + 'COLLATE "C"'
)
_NORMALIZED_LICENCE_STATE = (
    f'regexp_replace(lower(licence_state COLLATE "C"), {_STRIP_SPACE_AND_PUNCTUATION}) COLLATE "C"'
)


@dataclass(frozen=True, slots=True)
class CompositeCase:
    batch_id: int
    equivalent_ids: frozenset[str]
    different_id: str


@pytest.fixture(scope="module")
def composite_case(settings: Settings) -> Iterator[CompositeCase]:
    """Insert two normalized equivalents and one genuinely different HCP."""
    rows = (
        {
            "source_key": "T079-BASE",
            "first_name": "Jean-Luc",
            "last_name": "O'Neil",
            "postal_code": "02139-1234",
            "licence_state": "M.A.",
        },
        {
            "source_key": "T079-EQUIVALENT",
            "first_name": "  j e a n",
            "last_name": " o neil ",
            "postal_code": " 02139 ",
            "licence_state": " ma ",
        },
        {
            "source_key": "T079-DIFFERENT",
            "first_name": "Jean",
            "last_name": "O'Neil",
            "postal_code": "02140",
            "licence_state": "MA",
        },
    )

    with db.connect(settings, Role.INGEST) as conn:
        source_system_id = int(
            conn.execute(
                text(
                    "INSERT INTO source_system (code, name) "
                    "VALUES ('T079-NORM', 'T079 normalization') RETURNING source_system_id"
                )
            ).scalar_one()
        )
        batch_id = int(
            conn.execute(
                text("""
                    INSERT INTO data_batch
                        (source_system_id, arrival_ts, business_period, record_count)
                    VALUES
                        (:source, :arrival,
                         daterange(CAST(:period_start AS date), CAST(:period_end AS date), '[)'),
                         :record_count)
                    RETURNING batch_id
                """),
                {
                    "source": source_system_id,
                    "arrival": datetime(2026, 10, 1, 12, tzinfo=UTC),
                    "period_start": date(2026, 9, 1),
                    "period_end": date(2026, 10, 1),
                    "record_count": len(rows),
                },
            ).scalar_one()
        )

        ids: dict[str, str] = {}
        for row in rows:
            hcp_id = conn.execute(
                text("""
                    INSERT INTO hcp
                        (source_system_id, source_key, valid_from, batch_id, is_deleted,
                         first_name, last_name, postal_code, licence_state)
                    VALUES
                        (:source_system_id, :source_key, :valid_from, :batch_id, false,
                         :first_name, :last_name, :postal_code, :licence_state)
                    RETURNING hcp_id
                """),
                {
                    **row,
                    "source_system_id": source_system_id,
                    "valid_from": date(2026, 9, 1),
                    "batch_id": batch_id,
                },
            ).scalar_one()
            ids[str(row["source_key"])] = str(hcp_id)

    yield CompositeCase(
        batch_id=batch_id,
        equivalent_ids=frozenset({ids["T079-BASE"], ids["T079-EQUIVALENT"]}),
        different_id=ids["T079-DIFFERENT"],
    )


def test_normalized_duplicate_rows_remain_insertable(
    settings: Settings, composite_case: CompositeCase
) -> None:
    """The expression index accelerates detection; it must never enforce uniqueness."""
    statement = text(
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (
                   WHERE {_NORMALIZED_LAST_NAME} = 'oneil'
                     AND {_NORMALIZED_FIRST_INITIAL} = 'j'
                     AND {_NORMALIZED_POSTAL_CODE} = '02139'
                     AND {_NORMALIZED_LICENCE_STATE} = 'ma'
               ) AS equivalent
        FROM hcp
        WHERE batch_id = :batch_id
        """
    )
    with db.connect(settings, Role.ENGINE) as conn:
        row = conn.execute(statement, {"batch_id": composite_case.batch_id}).one()

    assert row.total == 3
    assert row.equivalent == 2


def test_composite_rule_matches_equivalent_formatting_only(
    settings: Settings, composite_case: CompositeCase
) -> None:
    """Case, whitespace, punctuation, first-name and ZIP formatting do not change identity."""
    definition = load_definition(RULE_PATH)
    with db.connect(settings, Role.ENGINE) as conn:
        rows = conn.execute(
            text(definition.predicate_sql),
            {
                "batch_id": composite_case.batch_id,
                "as_of_date": AS_OF_DATE,
                "reference_watermark": composite_case.batch_id,
            },
        ).all()

    matched = {str(row.subject_key) for row in rows}
    assert matched == set(composite_case.equivalent_ids)
    assert composite_case.different_id not in matched
