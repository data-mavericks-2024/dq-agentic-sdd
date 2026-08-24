# Quickstart — Deterministic Data-Quality Foundation

How to prove this feature works end to end. Validation guide, not implementation guide.

**Revision 2** — scenarios corrected after the plan review. The missing-feed scenario no longer runs
against an unrelated batch, the seed spans three periods so the period-based rules can fire at all,
and scenarios were added for reproducibility and role conformance.

## Prerequisites

- Python 3.12 and `uv` on PATH. If `uv` is not recognised in a VS Code terminal, that terminal
  started before uv was installed — run `$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"` or
  restart VS Code entirely.
- `.env` populated from `.env.example`. `SUPABASE_DB_STATEFUL_URL` must be the **session pooler**
  (port 5432 on `...pooler.supabase.com`), not the transaction pooler.
- The Supabase project awake. Free-tier projects pause after roughly a week of inactivity; a
  connection failure after a quiet week is usually a paused project, not a broken configuration.

```powershell
uv run --with "psycopg[binary]" python scripts/check_db.py
```

Expect `prepared statements: OK`, `advisory locks: OK`, `CREATE ROLE: OK`.

## Setup

```powershell
uv sync
uv run alembic upgrade head
```

Creates the five schemas, the tables, the determinism trigger, and — via the bootstrap migration
that runs only when no schema prefix is set — the seven database roles. Capture the seven
`DQ_*_URL` connection strings into `.env` afterwards; those roles do not exist until this runs.

## Scenario 1 — Detection is exact (SC-001)

The headline criterion. The finding set must *equal* the expected set, not merely contain it.

```powershell
uv run dq seed --periods 3 --with-defects
uv run dq run-rules --batch-id 1
uv run pytest tests/integration/test_golden_findings.py
```

**Expected:** the produced finding set equals the expected set emitted by the generator. All nine
rule families are covered.

Two things that look like failures and are not. The **namesake pair** — two genuinely distinct HCPs
sharing surname, first initial, postal code, and licence state — is flagged by FR-016b and is an
*expected* finding; that is why SC-001 asserts set equality rather than "zero false positives". And
the expected set is **emitted by the generator**, not hand-maintained, so it cannot drift out of
step with the data.

Three periods are seeded because FR-022 needs a prior period to compare against and FR-021b needs a
period where an expected feed is absent. One batch cannot exercise either.

## Scenario 2 — Re-running changes nothing (SC-003, FR-011)

```powershell
uv run dq run-rules --batch-id 1
uv run dq summarise --batch-id 1
```

**Expected:** identical counts. Zero new findings. A second `rule_run` row records that the run
happened; the finding set is untouched.

This holds because of a database constraint, so it also holds for a run killed halfway. Verify by
interrupting a run with Ctrl-C and re-running.

## Scenario 3 — A historical re-run survives later deliveries (SC-010)

**The scenario that distinguishes idempotency from reproducibility**, and the one revision 1 could
not have passed.

```powershell
uv run dq run-rules --batch-id 1
uv run dq seed --periods 1 --amend-master     # a later batch changing master data
uv run dq run-rules --batch-id 1              # re-run the ORIGINAL period
uv run pytest tests/integration/test_reproducibility.py
```

**Expected:** the finding set for batch 1 is unchanged, because the run resolves master data as of
batch 1's period and filters to the reference watermark recorded on the original run.

Without that pinning, the second run would see master data that did not exist when the first ran,
and findings would appear in or vanish from a historical batch — while Scenario 2 still passed.
That is exactly what makes this failure mode dangerous: it is invisible to the obvious test.

## Scenario 4 — Rule versioning preserves history (SC-005, FR-006)

```powershell
uv run dq rules register specs/001-data-foundation/examples/volume-deviation-v2.yaml
uv run dq run-rules --batch-id 2
```

**Expected:** a new `rule_version` at `version_no: 2`. Version 1's findings remain, still pointing
at version 1 with its original threshold and severity.

Check the summary too: counts are grouped **per rule**, not per rule version, so the same real
problem is not reported twice after a threshold change.

## Scenario 5 — Deactivation is not deletion (SC-006, FR-007)

```powershell
uv run dq rules deactivate HCP-NPI-FORMAT
uv run dq run-rules --batch-id 3
uv run dq findings --batch-id 1 --rule HCP-NPI-FORMAT
```

**Expected:** no new findings on batch 3; batch 1's historical findings still retrievable.

## Scenario 6 — The engine cannot write commercial data (Constitution II, VI)

The most important test in this feature.

```powershell
uv run pytest tests/integration/test_role_privileges.py
```

**Expected:** as `dq_engine`, every attempted write to `commercial` — `INSERT`, `UPDATE`, `DELETE`,
`CREATE TABLE` — raises `InsufficientPrivilege`. As `dq_author`, every `SELECT` on `commercial`
fails. And `run_rules` refuses to start if `current_user` is not `dq_engine`.

**If this passes only because the code never attempts a write, it is worthless.** It must attempt
the write and be refused.

## Scenario 7 — The role set matches the constitution (Constitution VI)

```powershell
uv run pytest tests/integration/test_role_conformance.py
```

**Expected:** the `dq_*` roles present in the database equal exactly the seven named in
`.specify/memory/constitution.md` v1.1.0. An eighth role fails this test, and the fix is to amend
the constitution — not the test. This is what stops least privilege eroding one reasonable role at
a time.

## Scenario 8 — Missing feeds come from expectation, not history (SC-009)

```powershell
uv run dq run-rules --source VEEVA --period 2026-09
```

**Expected:** a finding with `subject_type: source_period` for a declared feed with no batch in that
period — including a feed that has never delivered anything.

Note the scope: `--source ... --period ...`, not `--batch-id`. A never-arrived feed has no batch, so
a batch-scoped run cannot express the question. Revision 1 ran this against an unrelated batch,
which also meant the same missing period re-fired against every subsequent batch.

## Scenario 9 — Predicates are genuinely deterministic (Constitution I)

```powershell
uv run pytest tests/integration/test_predicate_determinism.py
```

**Expected:** every registered rule's predicate returns byte-identical rows across executions under
varied query plans (`max_parallel_workers_per_gather` 0 and 4, `enable_indexscan` on and off).

**This compares predicate output, not persisted findings.** Comparing findings would pass
unconditionally: `ON CONFLICT DO NOTHING` discards a second run's differing row for the same
subject, so the stored set is identical by construction rather than by determinism. Revision 1's
version of this test could never have failed.

## Scenario 10 — Performance at target scale (SC-008)

Excluded from the default suite. Generates its dataset server-side rather than shipping a million
rows across the network.

```powershell
uv run pytest -m volume
```

**Expected:** a full rule run over ~1M sales transactions and ~100K HCP versions completes in
**under 10 minutes**.

**Run this early.** It is the check that catches an architecturally wrong design, and its value is
entirely in catching it now rather than at Feature 5. Two ways it can fail: per-record evaluation
somewhere, or free-tier `t4g.nano` simply being unable. Both are worth knowing immediately.

**Check the storage arithmetic before writing it.** 1M transactions with four indexes plus 100K
versioned HCP rows with three plausibly consumes 250–350 MB of a 500 MB ceiling before findings.
The test may be infeasible rather than merely slow.

## Full validation

```powershell
uv run pytest                    # default: fast, no volume test
uv run pytest -m volume          # the SC-008 check
uv run ruff check . ; uv run mypy .
```

## Test isolation

Integration tests share the Supabase project with development. Each session creates
`test_<YYYYMMDDHHMMSS>_<uuid6>_commercial` and its four siblings, migrates into them, and drops them
on teardown. Session start sweeps any `test_*` schema whose **embedded timestamp** is older than
four hours — PostgreSQL records no schema creation time, so the timestamp has to be in the name.

**Test sessions never create or drop roles.** Roles are cluster-global and cannot be schema-isolated;
the bootstrap migration that creates them is skipped whenever a schema prefix is set. A test run that
dropped them would break development outright.

Manual cleanup:

```powershell
uv run dq admin sweep-test-schemas --older-than 0
```

**No test writes outside its own prefixed schemas.** A test touching unprefixed `commercial` is a
defect, not a slow test.
