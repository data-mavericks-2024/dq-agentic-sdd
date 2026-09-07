"""Replace the raw HCP composite index with normalized expressions (T079).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-07

The index is deliberately non-unique. Matching composite values are evidence of a possible
duplicate that the deterministic rule must surface; rejecting the second row would hide the defect.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op
from sqlalchemy import text

from dq.db.migration_support import assume_migrate_role, phys, reset_role
from dq.db.schemas import Schema

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPOSITE_MATCH_INDEX: Final[str] = "ix_hcp_composite_match"

_NORMALIZED_INDEX = """
CREATE INDEX "{index}" ON "{schema}".hcp (
  (regexp_replace(lower(last_name COLLATE "C"), '[[:space:][:punct:]]+', '', 'g') COLLATE "C"),
  (left(regexp_replace(lower(first_name COLLATE "C"), '[[:space:][:punct:]]+', '', 'g'), 1) COLLATE "C"),
  (left(regexp_replace(lower(postal_code COLLATE "C"), '[[:space:][:punct:]]+', '', 'g'), 5) COLLATE "C"),
  (regexp_replace(lower(licence_state COLLATE "C"), '[[:space:][:punct:]]+', '', 'g') COLLATE "C")
)
"""

_RAW_INDEX = """
CREATE INDEX "{index}" ON "{schema}".hcp (
  last_name COLLATE "C",
  left(first_name, 1) COLLATE "C",
  postal_code COLLATE "C",
  licence_state COLLATE "C"
)
"""


def _replace_index(definition: str) -> None:
    conn = op.get_bind()
    assume_migrate_role(conn)
    commercial = phys(Schema.COMMERCIAL)
    try:
        conn.execute(text(f'DROP INDEX "{commercial}"."{COMPOSITE_MATCH_INDEX}"'))
        conn.execute(text(definition.format(index=COMPOSITE_MATCH_INDEX, schema=commercial)))
    finally:
        reset_role(conn)


def upgrade() -> None:
    _replace_index(_NORMALIZED_INDEX)


def downgrade() -> None:
    _replace_index(_RAW_INDEX)
