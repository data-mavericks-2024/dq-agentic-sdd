"""Predicates return the same rows under different query plans (T062, principle I).

**This test compares predicate output, never persisted findings**, and that distinction is the
entire reason it exists in this form.

The first design proposed "run each rule twice against a fixed batch and assert the finding sets are
equal". That test could never fail. ``ON CONFLICT DO NOTHING`` silently discards the second run's
row whenever a subject already has one — so the persisted set is byte-identical across runs *by
construction*, not by determinism. The idempotency mechanism guarantees the test passes; the
non-determinism it exists to catch is precisely what the mechanism hides (research.md D11).

So each predicate is executed directly, into two result sets, with the full row compared including
``offending_value``. And the two executions are made to differ in **query plan**, because that is
where the hazards live:

* ``max_parallel_workers_per_gather`` — parallel aggregation can change combine order.
* ``enable_indexscan`` — an index scan and a sequential scan of the same predicate disagree if a
  collation has shifted underneath the index. This is why the FR-016b composite columns are
  ``COLLATE "C"``.

Two identical executions seconds apart certify nothing. Two executions under different plans are
the cheapest available approximation of "the same question, asked differently".
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection, text

from dq.config.settings import Role, Settings
from dq.db import engine as db
from dq.engine.runner import BatchScope, resolve_scope
from dq.rules.predicates import bound_parameter_names
from dq.rules.registry import active_versions
from dq.seed.generator import TARGET_PERIOD, TARGET_SOURCE, SeedResult

pytestmark = pytest.mark.usefixtures("registered")

#: Plan-shaping settings, applied per execution. The pair is chosen to force genuinely different
#: plans rather than different timings.
PLAN_VARIANTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("serial + indexes", {"max_parallel_workers_per_gather": "0", "enable_indexscan": "on"}),
    ("parallel + seqscan", {"max_parallel_workers_per_gather": "4", "enable_indexscan": "off"}),
)


def _execute_predicate(
    conn: Connection, predicate_sql: str, bindings: dict[str, Any], plan: dict[str, str]
) -> list[tuple[Any, ...]]:
    """Run one predicate under one plan shape and return its rows, sorted.

    Sorted because the contract is about the *set* of failing subjects, not the order they come
    back in — a predicate is not required to impose a total order on its output, only on any
    tie-break that decides which row survives (rule 6).
    """
    for key, value in plan.items():
        conn.execute(text("SELECT set_config(:k, :v, true)"), {"k": key, "v": value})

    rows = conn.execute(text(predicate_sql), bindings).all()
    return sorted(tuple(r) for r in rows)


@pytest.fixture(scope="module")
def evaluable(settings: Settings, seeded: SeedResult, registered: list[str]) -> list[Any]:
    """Every active record-level rule version, with the bindings the engine would supply."""
    batch = seeded.batch(TARGET_SOURCE, TARGET_PERIOD[0])
    with db.connection(settings, Role.ENGINE) as conn, conn.begin():
        db.pin_session(conn, settings.schema_prefix)
        resolved = resolve_scope(conn, BatchScope(batch.batch_id))
        versions = active_versions(conn, resolved.subject_types)
    return [(v, resolved) for v in versions]


def test_there_are_rules_to_check(evaluable: list[Any]) -> None:
    """Guards every parametrised case below from passing on an empty list."""
    assert len(evaluable) >= 7, f"expected the record-level library, got {len(evaluable)} rules"


def test_every_predicate_is_stable_across_query_plans(
    settings: Settings, evaluable: list[Any]
) -> None:
    """The same rows, under both plan shapes, for every rule in the library."""
    engine_bindings = {
        "batch_id": None,
        "as_of_date": None,
        "reference_watermark": None,
        "scope_source_system_id": None,
        "scope_period_start": None,
        "scope_period_end": None,
    }

    differences: list[str] = []

    with db.connection(settings, Role.ENGINE) as conn:
        for version, resolved in evaluable:
            engine_bindings.update(
                batch_id=resolved.batch_id,
                as_of_date=resolved.as_of_date,
                reference_watermark=resolved.reference_watermark,
                scope_source_system_id=resolved.source_system_id,
                scope_period_start=resolved.period_start,
                scope_period_end=resolved.period_end,
            )
            available = {**version.parameters, **engine_bindings}
            needed = bound_parameter_names(version.predicate_sql)
            bindings = {name: available[name] for name in needed}

            results: dict[str, list[tuple[Any, ...]]] = {}
            for label, plan in PLAN_VARIANTS:
                # A fresh transaction per execution, so `set_config(..., true)` from one plan
                # cannot leak into the next.
                with conn.begin():
                    db.pin_session(conn, settings.schema_prefix)
                    results[label] = _execute_predicate(conn, version.predicate_sql, bindings, plan)

            first, second = (results[label] for label, _ in PLAN_VARIANTS)
            if first != second:
                only_first = [r for r in first if r not in second]
                only_second = [r for r in second if r not in first]
                differences.append(
                    f"{version.rule_key} v{version.version_no}: "
                    f"{len(first)} vs {len(second)} rows\n"
                    f"    only under {PLAN_VARIANTS[0][0]}: {only_first[:5]}\n"
                    f"    only under {PLAN_VARIANTS[1][0]}: {only_second[:5]}"
                )

    assert not differences, (
        "predicate output changed with the query plan — the rule is not deterministic:\n"
        + "\n".join(differences)
    )


def test_offending_values_are_compared_not_just_subject_keys(
    settings: Settings, evaluable: list[Any]
) -> None:
    """The comparison above includes every projected column, and this asserts that it must.

    Master-data joins can fan out, producing several rows for one subject that differ only in
    ``offending_value``, with plan order deciding which one the engine inserts. Comparing subject
    keys alone would call that deterministic. It is not — it is a coin flip that happens to land
    the same way twice.
    """
    with db.connection(settings, Role.ENGINE) as conn:
        for version, resolved in evaluable:
            if version.subject_type != "record":
                continue
            with conn.begin():
                db.pin_session(conn, settings.schema_prefix)
                columns = conn.execute(
                    text(f"SELECT * FROM ({version.predicate_sql}) p WHERE false"),
                    {
                        name: getattr(resolved, name, None)
                        or {
                            "batch_id": resolved.batch_id,
                            "as_of_date": resolved.as_of_date,
                            "reference_watermark": resolved.reference_watermark,
                            **version.parameters,
                        }.get(name)
                        for name in bound_parameter_names(version.predicate_sql)
                    },
                ).keys()
            assert "offending_value" in columns, (
                f"{version.rule_key} does not project offending_value, so the determinism "
                f"comparison above could not have included it"
            )
