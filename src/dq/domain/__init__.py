"""Table definitions for the ``commercial`` and ``dq`` schemas.

One :class:`~sqlalchemy.MetaData` holds both. Tables carry **symbolic** schema names; the physical
name is applied per connection by ``schema_translate_map`` (see :mod:`dq.db.schemas`), so nothing
here knows or cares whether it is running against development or an isolated test schema.
"""

from __future__ import annotations

from sqlalchemy import MetaData

#: Naming convention so constraint and index names are stable across migrations and assertable by
#: name in tests/integration/test_schema_indexes.py (T018).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

__all__ = ["NAMING_CONVENTION", "metadata"]
