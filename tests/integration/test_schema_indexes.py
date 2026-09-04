"""Fresh-head index conformance, including FR-016b normalization (T018, T085)."""

from __future__ import annotations

import json

from sqlalchemy import Engine, text

from dq.db.schemas import Schema
from dq.domain.commercial import (
    COMPOSITE_MATCH_INDEX,
)
from dq.domain.commercial import (
    EXPECTED_INDEXES as COMMERCIAL_INDEXES,
)
from dq.domain.dq import EXPECTED_INDEXES as DQ_INDEXES

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


def test_every_declared_index_exists(admin_engine: Engine, phys_schema: dict[Schema, str]) -> None:
    expected = set(COMMERCIAL_INDEXES) | set(DQ_INDEXES)
    with admin_engine.connect() as conn:
        present = {
            str(row.relname)
            for row in conn.execute(
                text(
                    "SELECT c.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind IN ('i', 'I') AND n.nspname IN (:commercial, :dq)"
                ),
                {
                    "commercial": phys_schema[Schema.COMMERCIAL],
                    "dq": phys_schema[Schema.DQ],
                },
            )
        }

    assert expected <= present, (
        f"declared indexes missing from fresh head: {sorted(expected - present)}"
    )


def test_composite_match_index_has_four_c_collated_expression_keys(
    admin_engine: Engine, phys_schema: dict[Schema, str]
) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.indnkeyatts, i.indisvalid, i.indisready, "
                "       i.indkey::text AS key_attributes, "
                "       array_agg(coll.collname ORDER BY key.position) AS collations "
                "FROM pg_index i "
                "JOIN pg_class idx ON idx.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = idx.relnamespace "
                "CROSS JOIN LATERAL "
                "     unnest(i.indcollation::oid[]) WITH ORDINALITY AS key(collation_oid, position) "
                "JOIN pg_collation coll ON coll.oid = key.collation_oid "
                "WHERE n.nspname = :schema AND idx.relname = :index "
                "GROUP BY i.indnkeyatts, i.indisvalid, i.indisready, i.indkey"
            ),
            {
                "schema": phys_schema[Schema.COMMERCIAL],
                "index": COMPOSITE_MATCH_INDEX,
            },
        ).one()

    assert row.indnkeyatts == 4
    assert row.indisvalid and row.indisready
    assert row.key_attributes == "0 0 0 0", "all four keys must be normalized expressions"
    assert list(row.collations) == ["C", "C", "C", "C"]


def test_composite_match_index_uses_exact_normalized_expressions(
    admin_engine: Engine, phys_schema: dict[Schema, str]
) -> None:
    """The planner can use the index only when the query expressions match its parse tree."""
    commercial = phys_schema[Schema.COMMERCIAL]
    statement = text(
        f'''EXPLAIN (FORMAT JSON, COSTS OFF)
            SELECT hcp_id
            FROM "{commercial}".hcp
            WHERE {_NORMALIZED_LAST_NAME} = 'smith'
              AND {_NORMALIZED_FIRST_INITIAL} = 'j'
              AND {_NORMALIZED_POSTAL_CODE} = '02139'
              AND {_NORMALIZED_LICENCE_STATE} = 'ma' '''
    )

    with admin_engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan: object = conn.execute(statement).scalar_one()

    assert COMPOSITE_MATCH_INDEX in json.dumps(plan), (
        f"{COMPOSITE_MATCH_INDEX} cannot serve the exact FR-016b/FR-016c normalized expressions"
    )
