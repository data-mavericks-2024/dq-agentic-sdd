---

description: "Task list for 001-data-foundation"
---

# Tasks: Deterministic Data-Quality Foundation

**Input**: Design documents from `/specs/001-data-foundation/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Test tasks ARE included. Constitution v1.1.0 § Development Workflow requires contract
tests, integration tests against a seeded database, and a golden scenario set for every feature —
tests are mandatory here, not optional.

**Organization**: Tasks are grouped by user story so each can be implemented and tested
independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)

## Path Conventions

Single project: `src/dq/`, `tests/`, `migrations/` at repository root, per plan.md § Source Code.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and the two facts that must be known before anything is built.

- [X] T001 Create the directory tree from plan.md § Source Code under `src/dq/`, `tests/`, `migrations/`
- [X] T002 Initialize the uv project in `pyproject.toml` with Python 3.12, SQLAlchemy Core, Alembic, psycopg[binary], Pydantic v2, pytest, ruff, mypy
- [X] T003 [P] Configure ruff and mypy strict in `pyproject.toml`
- [X] T004 [P] Register pytest markers `volume` and `golden` in `pyproject.toml`, and exclude `volume` from the default run
- [X] T005 Implement settings loading in `src/dq/config/settings.py`: connection URLs per role, `DQ_SCHEMA_PREFIX`, fail-fast on a missing required variable
- [X] T006 [P] Create the model-ID config placeholder in `src/dq/config/models.py` — unused until Feature 3, but the module must exist so no ID is ever inlined later
- [X] T007 **Establish the volume-test storage budget** and record it in `specs/001-data-foundation/research.md` § D8: measure per-row bytes for `sales_transaction` and `hcp` with their indexes, extrapolate to 1M + 100K rows, and state plainly whether the SC-008 test fits inside the 500 MB free tier
- [X] T008 [P] Add the seven `DQ_*_URL` entries to `.env.example` with a note that they do not exist until T012 runs

**Checkpoint**: T007 may return "the volume test is infeasible on free tier". That is a valid,
useful outcome — resolve it (drop indexes for the test, lower the stated target, or accept paid
compute) before T052 rather than discovering it there.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Schema, roles, triggers, and the test harness. Every user story depends on all of it.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Migration framework and roles

- [X] T009 Initialize Alembic and make `migrations/env.py` schema-prefix aware, reading `DQ_SCHEMA_PREFIX` and setting `version_table_schema` accordingly
- [X] T010 [P] Implement schema-name resolution in `src/dq/db/schemas.py` — the single place `${P}` is applied
- [X] T011 Implement the per-role connection factory in `src/dq/db/engine.py`, including the `SELECT current_user` assertion and the `SET LOCAL` pinning of `TimeZone`, `DateStyle`, `search_path`, and `statement_timeout` (research.md D10)
- [X] T012 Write the bootstrap migration creating the seven roles (`dq_migrate`, `dq_ingest`, `dq_author`, `dq_engine`, `dq_readonly`, `dq_sandbox`, `dq_publish`) with `LOGIN NOINHERIT` and passwords supplied as bound parameters — **skipped entirely when `DQ_SCHEMA_PREFIX` is non-empty**, because roles are cluster-global and a prefixed test run must never create or drop them

### Schema

- [X] T013 Migration creating the five schemas `${P}commercial`, `${P}dq`, `${P}workflow`, `${P}audit`, `${P}sandbox`
- [X] T014 [P] Define `source_system` and `data_batch` in `src/dq/domain/commercial.py` per data-model.md, including the `record_count >= 0` check
- [X] T015 [P] Define the four master tables (`hcp`, `hco`, `product`, `territory`) in `src/dq/domain/commercial.py` as append-only version chains: `(source_system_id, source_key, valid_from)` unique, `is_deleted`, `batch_id`, **no `valid_to`**
- [X] T016 [P] Define `territory_alignment` in `src/dq/domain/commercial.py` with a `daterange` `[from, to)` and **no exclusion constraint** — an overlap must be insertable or FR-019 cannot be tested
- [X] T017 [P] Define `sales_transaction` in `src/dq/domain/commercial.py` with `product_key`/`hcp_key` as **text, not foreign keys**, and `quantity numeric(18,4)`
- [X] T018 Add all indexes from data-model.md, including `COLLATE "C"` on the four FR-016b composite-match columns. **Done when** `tests/integration/test_schema_indexes.py` asserts each expected index exists by name and that the composite-match columns carry `COLLATE "C"`
- [X] T019 Add the immutability trigger rejecting `UPDATE`/`DELETE` on `data_batch` **and on every batch member table** — the container alone is not enough (FR-003b)
- [X] T020 [P] Define `rule` and `rule_version` in `src/dq/domain/dq.py`, with all four declarations `NOT NULL` (FR-005) and `UNIQUE (rule_id, version_no)`
- [X] T021 [P] Define `feed_expectation` in `src/dq/domain/dq.py`
- [X] T022 [P] Define `rule_run` and `rule_run_rule_version` in `src/dq/domain/dq.py`, including `correlation_id`, `as_of_date`, `reference_watermark`, `session_settings`, and the `COMPLETED_WITH_ERRORS` status value
- [X] T023 Define `finding` in `src/dq/domain/dq.py` with nullable `batch_id`, non-null `scope_key`, `UNIQUE (rule_version_id, scope_key, subject_key)`, and both `CHECK` constraints from data-model.md
- [X] T024 Add the immutability trigger on `finding` — it is the audit-bearing table
- [X] T025 Add the `BEFORE INSERT` trigger on `rule_version` enforcing all eight predicate validation rules from contracts/rule-definition.md, including `pg_proc.provolatile = 'i'` for every expression function, **and rejecting similarity functions by name** (`levenshtein`, `similarity`, `soundex`, `metaphone`, `difference`, `word_similarity`, and the pg_trgm `%` and `<->` operators) per FR-016d — a deterministic `levenshtein(a,b) < 3` is IMMUTABLE and passes every other check. **This must be in the database, not only in Python** — `dq_author` can insert here directly
- [X] T026 Apply the full grant matrix and `ALTER DEFAULT PRIVILEGES` for every schema and verb from data-model.md; issue **no** `audit`/`workflow` grant to `dq_readonly` (deferred to Feature 3)

### Test harness

- [X] T027 Implement the test schema lifecycle in `tests/conftest.py`: create `test_<YYYYMMDDHHMMSS>_<uuid6>_*` schemas, apply migrations into them, drop on teardown
- [X] T028 Implement the stale-schema sweep in `tests/conftest.py`, parsing the **timestamp embedded in the schema name** (PostgreSQL records no schema creation time) and skipping any prefix listed in the live-session registry; this completed implementation evidence is superseded by the migration-compliant advisory-lock design in T077
- [X] T029 [P] Write the role conformance test in `tests/integration/test_role_conformance.py` asserting the `dq_*` roles present equal exactly the seven in constitution v1.1.0 — an eighth role must fail the build
- [X] T030 [P] Write the role privilege test in `tests/integration/test_role_privileges.py`: as `dq_engine`, `INSERT`/`UPDATE`/`DELETE`/`CREATE TABLE` against `commercial` must each raise `InsufficientPrivilege`; as `dq_author`, `SELECT` on `commercial` must fail. **Each test must attempt the operation** — one that passes because nothing was attempted proves nothing

**Checkpoint**: Foundation ready. T029 and T030 must pass before any user story begins — they are
the structural enforcement of constitution principles II and VI.

---

## Phase 3: User Story 1 — Steward sees exactly what failed (Priority: P1) 🎯 MVP

**Goal**: A full rule run over a seeded batch produces record-level findings, each naming the
failing record, the offending value, the rule, and the rule version.

**Independent Test**: Seed the dataset with injected defects, run all rules, and assert the produced
finding set equals the generator-emitted expected set (SC-001).

### Tests for User Story 1 ⚠️

> Write these first and confirm they fail before implementing.

- [X] T031 [P] [US1] Unit tests for rule-definition validation in `tests/unit/test_rule_definition.py` — one failing case per validation rule, including a predicate containing `CURRENT_DATE`
- [X] T031a [P] [US1] Negative test in `tests/unit/test_rule_definition.py` asserting a predicate using `levenshtein`, `similarity`, or a pg_trgm operator is rejected at registration (FR-016d)
- [X] T032 [P] [US1] Contract test for the four-column predicate output contract in `tests/contract/test_predicate_contract.py`
- [X] T033 [P] [US1] Golden set-equality test in `tests/integration/test_golden_findings.py` asserting produced findings == expected findings across all nine rule families (SC-001)
- [X] T033a [P] [US1] Integration test in `tests/integration/test_missing_feed.py` asserting a declared feed with no batch in a period produces a `source_period` finding — including a feed that has never delivered anything since being declared (SC-009, FR-021d)

### Implementation

- [X] T034 [P] [US1] Implement the `RuleDefinition` Pydantic model in `src/dq/rules/definition.py` per contracts/rule-definition.md
- [X] T035 [US1] Implement predicate parsing and the eight validation rules in `src/dq/rules/predicates.py`, mirroring the T025 trigger so failures surface as `RuleDefinitionError` before reaching the database
- [X] T035a [US1] Shared-corpus agreement test in `tests/integration/test_validation_parity.py`: one corpus of invalid predicates, each asserted rejected by **both** the Python validator (T035) and the database trigger (T025). Drift between the two is otherwise silent, and the trigger is the one that actually enforces constitution principle I
- [X] T036 [US1] Implement registry registration and lookup in `src/dq/rules/registry.py` (version creation deferred to US2)
- [X] T037 [US1] Implement scope resolution in `src/dq/engine/runner.py`: `BatchScope` and `SourcePeriodScope`, deriving `as_of_date` and `reference_watermark` and writing them to `rule_run`
- [X] T038 [US1] Implement rule execution in `src/dq/engine/runner.py`: advisory lock per `(scope_key, rule_version_id)`, then one `INSERT … SELECT … ON CONFLICT DO NOTHING` per rule version
- [X] T039 [US1] Implement per-rule error capture in `src/dq/engine/runner.py`: record `ERRORED` with detail, continue remaining rules, and close the run `COMPLETED_WITH_ERRORS` rather than `COMPLETED`
- [X] T040 [P] [US1] Rule FR-015 — missing or structurally invalid NPI — in `src/dq/rules/library/hcp_npi_format.yaml`
- [X] T041 [P] [US1] Rule FR-016a — HCP records sharing an NPI under distinct surrogate keys, High severity — in `src/dq/rules/library/hcp_dup_npi.yaml`
- [X] T042 [P] [US1] Rule FR-016b — HCP composite match on last name, first initial, postal code, licence state, Medium severity — in `src/dq/rules/library/hcp_dup_composite.yaml`
- [X] T043 [P] [US1] Rule FR-017 — sales referencing a product or HCP absent from master data — in `src/dq/rules/library/sales_orphan_ref.yaml`
- [X] T044 [P] [US1] Rule FR-018 — sales attributed to a territory with no active alignment on the transaction date — in `src/dq/rules/library/sales_no_alignment.yaml`
- [X] T045 [P] [US1] Rule FR-019 — overlapping or gapped alignment effective ranges — in `src/dq/rules/library/alignment_overlap_gap.yaml`
- [X] T046 [P] [US1] Rule FR-020 — unit-of-measure inconsistency between sales and product master — in `src/dq/rules/library/sales_uom_mismatch.yaml`
- [X] T047 [P] [US1] Rule FR-021b — late or missing feed, `subject_type: source_period` — in `src/dq/rules/library/feed_late_missing.yaml`
- [X] T048 [P] [US1] Rule FR-022 — period-over-period volume deviation beyond a per-rule threshold — in `src/dq/rules/library/volume_deviation.yaml`
- [X] T049 [US1] Implement the synthetic data generator in `src/dq/seed/generator.py`: at least three consecutive periods, versioned master records, and **guaranteed composite-key uniqueness among non-defect HCPs** so FR-016b cannot collide by coincidence
- [X] T049a [US1] Seed the feed expectation catalogue in `src/dq/seed/generator.py`: at least two declared feeds, one delivering on cadence and one with a deliberately absent period (FR-021a)
- [X] T050 [US1] Implement defect injection in `src/dq/seed/defects.py`, **emitting the expected finding set as it injects** rather than relying on a hand-maintained list; include the namesake pair required by the spec's edge cases; and seed one **rule-is-wrong** scenario — a volume-deviation rule whose threshold is stale relative to a legitimate business change, firing against data that is entirely correct. Constitution X requires that scenario in the golden set before any agent capability is built against it, and Feature 1 is where seed data exists; retrofitting it at Feature 3 means seeding backwards from an agent conclusion
- [X] T051 [US1] Implement `dq seed` and `dq run-rules` in `src/dq/cli.py`, supporting both `--batch-id` and `--source/--period` scopes
- [X] T052 [US1] Write the volume test in `tests/volume/test_rule_run_scale.py`, generating data server-side with `generate_series` and asserting a full run completes in under 10 minutes (SC-008)

**Checkpoint**: US1 is fully functional and independently testable. **Run T052 now, not later** —
it is the check that catches an architecturally wrong design, and its entire value lies in catching
it before three more stories are built on top.

---

## Phase 4: User Story 2 — Engineer governs the rule catalogue (Priority: P2)

**Goal**: Rules can be added, changed, and retired without rewriting the meaning of findings already
recorded.

**Independent Test**: Register a rule, run it, change its predicate, run again, and confirm each
run's findings are attributable to the correct version and that earlier findings still describe what
was true when they were produced.

### Tests for User Story 2 ⚠️

- [X] T053 [P] [US2] Unit test in `tests/unit/test_version_semantics.py` classifying which field changes create a version and which do not
- [X] T054 [P] [US2] Integration test in `tests/integration/test_rule_versioning.py` — a threshold change creates version 2, version 1's findings remain interpretable (SC-005)
- [X] T055 [P] [US2] Integration test in `tests/integration/test_rule_deactivation.py` — a deactivated rule produces no new findings and loses none historically (SC-006)
- [X] T055a [P] [US2] Integration test in `tests/integration/test_rule_extensibility.py`: register a rule family absent from the library, run it, assert findings are produced — with a guard asserting no file under `src/dq/engine/` was modified (SC-007). The no-diff guard is the part that matters; without it the test passes even if someone adds an engine branch to make the rule work
- [X] T055b [P] [US2] Integration test in `tests/integration/test_feed_expectation_retirement.py` asserting an end-dated feed expectation stops producing missing-feed findings for periods after its end date, while retaining those raised before it (FR-021c)

### Implementation

- [X] T056 [US2] Implement versioning semantics in `src/dq/rules/registry.py`: a change to `predicate_sql`, `parameters`, `severity`, or `subject_type` appends a version; a change to `owning_function` or `is_active` does not
- [X] T057 [US2] Implement activate and deactivate in `src/dq/rules/registry.py`, updating only `rule.is_active`
- [X] T058 [US2] Implement `dq rules register` and `dq rules deactivate` in `src/dq/cli.py`, connecting as `dq_author`

**Checkpoint**: US1 and US2 both work independently.

---

## Phase 5: User Story 3 — Any historical run can be reproduced (Priority: P3)

**Goal**: Explicitly replaying a successfully completed historical run yields the same evaluated
findings — not similar ones, the same ones — even after later data has arrived; a normal scoped run
remains a distinct fresh evaluation.

**Independent Test**: Run a period to successful completion, deliver a later batch that changes
master data, explicitly replay the original `rule_run_id`, and assert evaluated set equality without
duplicate persisted findings; separately assert that a fresh evaluation sees the later world
(SC-010).

### Tests for User Story 3 ⚠️

- [X] T059 [P] [US3] Integration test in `tests/integration/test_idempotent_rerun.py` — re-running a scope creates no duplicate findings (SC-003)
- [X] T060 [P] [US3] Integration test in `tests/integration/test_reproducibility.py` — a historical re-run after a later master-data delivery produces an identical finding set (SC-010). **This is the test revision 1's design would have failed; T081 supersedes its completion evidence with explicit replay-versus-fresh coverage.**
- [X] T061 [P] [US3] Integration test in `tests/integration/test_crash_resume.py` — a run interrupted mid-flight and re-executed yields the same finding set as an uninterrupted run
- [X] T062 [P] [US3] Determinism test in `tests/integration/test_predicate_determinism.py` comparing **predicate output rows**, not persisted findings, across executions with `max_parallel_workers_per_gather` at 0 and 4 and `enable_indexscan` on and off

### Implementation

- [X] T063 [US3] Add `--amend-master` to `src/dq/seed/generator.py`, delivering a later batch that changes an existing master record so T060 has something to prove
- [X] T064 [US3] Extend the T025 trigger and `src/dq/rules/predicates.py` to **reject any predicate referencing a master table without binding both `:as_of_date` and `:reference_watermark`** (research.md R2) — a predicate omitting the watermark returns plausible results and still passes T062, so only a structural check catches it

**Checkpoint**: The reproducibility guarantee is verified rather than asserted.

---

## Phase 6: User Story 4 — Findings can be sliced and summarised (Priority: P4)

**Goal**: A steward filters findings by any combination of dimensions and reads a per-batch summary
whose counts reconcile exactly.

**Independent Test**: With the seeded dataset evaluated, filter on each dimension independently and
confirm counts reconcile against the summary.

### Tests for User Story 4 ⚠️

- [ ] T065 [P] [US4] Integration test in `tests/integration/test_findings_query.py` covering each filter and their combinations (FR-023), and asserting every returned finding includes `finding_id`, subject identity, offending or observed value, expected value, `rule_key`, `rule_version_id`, `version_no`, domain, dimension, severity, `owning_function`, and `detected_at` so record findings are fully attributable without a separate lookup (US1/AC4, SC-002)
- [ ] T066 [P] [US4] Integration test in `tests/integration/test_batch_summary.py` — counts reconcile exactly (SC-004), counts are **per rule not per rule version** after a version change, and errored rules appear as an explicit line
- [ ] T066a [P] [US4] Integration test in `tests/integration/test_summary_aggregates.py` asserting a never-arrived feed appears in the summary for the affected source and period — not only in a findings query (FR-024a, SC-009)
- [ ] T067 [P] [US4] Unit test for summary aggregation arithmetic in `tests/unit/test_summary_math.py`

### Implementation

- [ ] T068 [US4] Implement `query_findings` in `src/dq/engine/summary.py` with all five filters, any combination, and all-None returning everything; return a typed Pydantic finding response assembled through the `finding`, `rule_version`, and `rule` relationships so callers need no separate lookup for severity, ownership, rule identity, or rule-version attribution
- [ ] T069 [US4] Implement `summarise_scope` in `src/dq/engine/summary.py` (with a `summarise_batch` wrapper): group by domain, by rule, and by severity; count distinct `subject_key` per `rule_id` so a threshold change does not double-count; include a rules-errored line; and **include `source_period` findings covering the scope's source and period** so a never-arrived feed is visible in the summary rather than only in a query (FR-024a)
- [ ] T070 [US4] Implement `dq findings` and `dq summarise` in `src/dq/cli.py`

**Checkpoint**: All four user stories independently functional.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T071 [P] Implement `dq admin sweep-test-schemas` in `src/dq/cli.py`
- [ ] T072 [P] Extend and complete the `README.md` created by T089 with setup, the three Supabase connection modes, and the seven roles while preserving its verified rollback documentation
- [ ] T073 Run every scenario in `specs/001-data-foundation/quickstart.md` end to end and record actual against expected
- [ ] T074 `uv run ruff check . ; uv run ruff format . ; uv run mypy .` clean
- [ ] T075 **Post-implementation constitution check**: for each of the eleven principles in v1.1.0, cite the passing test or database object that enforces it, or flag it unenforced. Principle VII must still be reported as deferred to Feature 3 — do not let it drift into a claimed PASS
- [ ] T076 Record in `docs/conversation.md` what is stubbed, incomplete, or not working, plus only the environment-variable names and role capabilities that Feature 2 must evaluate; never record connection-string values, passwords, or contents of `.env` or `.env.bak`, and state explicitly that workflow and audit write-role grants remain unresolved Feature 2 planning decisions within the constitution's exhaustive seven-role model

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup — **blocks all user stories**
- **US1 (Phase 3)**: depends on Foundational
- **US2 (Phase 4)**: depends on Foundational; T056 extends T036 from US1
- **US3 (Phase 5)**: depends on US1 (needs a working runner to re-run)
- **Convergence (Phase 8)**: depends on Phase 5 and is a mandatory remediation gate; despite its
  append-only location, it MUST complete before Phase 6 or Phase 7, and implementation resumes at
  T077 rather than T065
- **US4 (Phase 6)**: depends on US1 and the Phase 8 convergence gate (needs remediated findings to query)
- **Polish (Phase 7)**: depends on all desired stories and the Phase 8 convergence gate

### Critical path notes

- **T007 before T052.** If the volume test cannot fit the free tier, know it in Phase 1.
- **T012 before T026 before anything connecting under a role.** Roles must exist before grants, and
  grants before any code assumes them.
- **T027 and T028 before every integration test.** Without the sweep, a hard-killed run silently
  accumulates schemas in the working database.
- **T029 and T030 before Phase 3.** They enforce constitution II and VI; a user story built before
  they pass is built on an unverified foundation.
- **T049a and T050 before T033 and T033a.** The expected finding set and the feed expectation
  catalogue must exist before the tests that assert against them — a missing-feed test cannot pass
  without a declared feed to be missing.
- **T064 is in US3 but guards US1's rules.** Consider pulling it forward if rule authoring starts
  before Phase 5 — a predicate written without the watermark will pass every US1 test.
- **T077 before any additional integration tests.** Test-session liveness and orphan cleanup must be
  trustworthy before more hosted-database tests run.
- **T085 before T079.** The fresh-head normalized-index assertions must fail before the composite
  normalization migration is implemented; within T079, write the upgrade-path test before the
  migration implementation.
- **T080 before T081.** Historical reads must enforce the watermark before replay is implemented.
- **T082, T083, and T084 before T073.** The documented CLI paths, clean seed, and baseline behavior
  must exist before the quickstart is executed end to end.
- **T065 and T068 before T073.** The finding query contract must be implemented and verified before
  the quickstart validation.
- **T089 before T072 before T073.** T089 creates the initial README with verified rollback guidance;
  T072 extends it without replacing that material before end-to-end validation.
- **T089 before T075.** The constitution audit cannot pass without a documented and verified
  rollback path.
- **T073, then T074, then T075.** End-to-end validation precedes static quality checks, and the
  post-implementation constitution audit is the final gate.

### Within each user story

Tests are written first and must fail. Domain models precede services; services precede the CLI.

### Parallel opportunities

- T003, T004, T006, T008 in Setup
- T010, T014–T017, T020–T022 in Foundational (different tables, one file each for domain modules —
  coordinate `commercial.py` edits or split by table)
- T029, T030 together
- T031, T031a, T032, T033, T033a together
- **T040–T048 — the nine rule definitions are nine separate YAML files with no interdependency.**
  This is the largest parallel block in the feature.
- T053–T055b, T059–T062, T065–T067 and T066a within their phases

---

## Parallel Example: User Story 1 rule library

```bash
# Nine independent rule definitions, one file each:
Task: "Rule FR-015 in src/dq/rules/library/hcp_npi_format.yaml"
Task: "Rule FR-016a in src/dq/rules/library/hcp_dup_npi.yaml"
Task: "Rule FR-016b in src/dq/rules/library/hcp_dup_composite.yaml"
Task: "Rule FR-017 in src/dq/rules/library/sales_orphan_ref.yaml"
Task: "Rule FR-018 in src/dq/rules/library/sales_no_alignment.yaml"
Task: "Rule FR-019 in src/dq/rules/library/alignment_overlap_gap.yaml"
Task: "Rule FR-020 in src/dq/rules/library/sales_uom_mismatch.yaml"
Task: "Rule FR-021b in src/dq/rules/library/feed_late_missing.yaml"
Task: "Rule FR-022 in src/dq/rules/library/volume_deviation.yaml"
```

---

## Implementation Strategy

### MVP — User Story 1 only

1. Phase 1 Setup, ending with a clear answer on volume-test feasibility (T007)
2. Phase 2 Foundational, ending with T029 and T030 green
3. Phase 3 US1, ending with T052
4. **Stop and validate**: SC-001 set equality passes and a full run fits inside 10 minutes

That is a shippable deterministic data-quality engine with no agents — the trustworthy baseline the
roadmap requires before any agent work begins.

### Incremental delivery

US1 → US2 → US3 → US4, each independently testable. US3 is mostly verification of a guarantee US1
implements, so it is cheap once US1 is solid; do not skip it, because the reproducibility guarantee
is what makes the whole thing defensible under inspection.

---

## Notes

- Constitution v1.1.0 governs. Where it and any artifact conflict, the constitution wins.
- Every task's "done when" is the named test passing or the named command running clean.
- Suffixed and convergence task IDs were added by analysis and convergence remediation rather than
  renumbering, so every existing reference stays valid.
- Three tasks look like ordinary hygiene and are not: **T028** (sweep) is the compensating control
  for sharing one Supabase project; **T064** (watermark binding) is the only structural defence
  against a rule that silently breaks historical reproducibility; and **T035a** (validation parity)
  is what stops the Python validator and the database trigger drifting apart, with the trigger being
  the one that actually enforces constitution principle I.
- Commit after each task or logical group.

---

## Phase 8: Convergence

- [X] T077 **CRITICAL** Add `migrations/versions/0010_remove_test_session_registry.py` to safely remove the obsolete runtime-created `public.dq_test_session` table from existing unprefixed databases while remaining skipped or harmless for prefixed test schemas, without editing an applied migration; replace its runtime creation and mutation in `tests/conftest.py` with PostgreSQL session advisory locks derived deterministically from the canonical `test_<YYYYMMDDHHMMSS>_<uuid6>_` prefix, hold each lock through a dedicated connection for the pytest session, make the startup sweep skip prefixes whose lock is held and remove orphaned prefixed schemas only after acquiring their lock, and add integration tests proving active sessions are preserved and crashed-session schemas are removed per Constitution Platform & Data Constraints / quickstart test isolation / T028 (contradicts)
- [X] T078 Extend `src/dq/rules/library/hcp_npi_format.yaml` with deterministic NPI check-digit validation, update the synthetic generator so non-defect NPIs are checksum-valid, and add malformed-checksum cases to the golden set per FR-015 (partial)
- [ ] T079 After the failing fresh-head index assertions in T085, implement the declared case-folding, whitespace and punctuation handling, and postal-code truncation in `src/dq/rules/library/hcp_dup_composite.yaml`; keep the normalization in versioned rule configuration, update the domain metadata, write a failing upgrade-path test, and add `migrations/versions/0011_composite_normalization_index.py` to replace the raw composite unique index with an expression index using the exact normalization rules without editing an applied migration; add equivalent-format rule tests, with T085 retaining ownership of fresh-head schema/index coverage, per FR-016b / FR-016c (partial)
- [ ] T080 Bind `:reference_watermark` into every historical predicate read that can observe later-delivered commercial rows, including `territory_alignment` and `data_batch`; update `src/dq/db/sql_objects.py` and add `migrations/versions/0012_historical_watermark_validation.py` so existing databases receive the validator and trigger behavior without editing an applied migration; add fresh-head and upgrade-path tests and extend structural predicate validation and reproducibility tests with later alignment and feed deliveries per FR-003d / SC-010 / T064 (partial)
- [ ] T081 Implement explicit historical replay through `dq run-rules --replay-of <rule_run_id>` and `migrations/versions/0013_rule_run_replay_lineage.py` without editing an applied migration: accept only an original run with status `COMPLETED`; derive its scope, `as_of_date`, `reference_watermark`, complete `session_settings`, and exact `rule_version_id` set from persisted records; reject `RUNNING`, `FAILED`, or `COMPLETED_WITH_ERRORS` sources and reject combining replay with scope or `--rule`; create a new run with nullable self-referencing `replay_of_rule_run_id` and restrictive deletion behavior, retain existing finding uniqueness, record replay rule outcomes in `rule_run_rule_version`, and compare finding-set equality over rule version, scope key, subject key, offending value, observed value, expected value, and severity rather than requiring duplicate replay-owned findings; test deterministic replay separately from a normal fresh evaluation per SC-010 / US3 (contradicts)
- [ ] T082 Add the documented `dq seed --periods` and `dq seed --amend-master` operational paths, including persisted-state reconstruction needed to amend an earlier seed, and add CLI tests for quickstart Scenarios 1 and 3 per quickstart / T063 (missing)
- [ ] T083 Make `seed(..., with_defects=False)` produce a genuinely clean world, including delivery of every active expected feed, and add an all-scope zero-findings integration test per US1/AC2 (partial)
- [ ] T084 Define volume-deviation scope as source and period with comparison grain `(product_key, territory_code)`; compare only grains present in both the current and immediately prior eligible period so a new product or territory combination without a baseline produces no finding, store the source-period deterministically in `scope_key` and the product-and-territory identity deterministically in `subject_key`, update the rule contract and seed data, and add tests proving both no-baseline suppression and a legitimate threshold breach per FR-022 / spec Edge Cases (partial)
- [ ] T085 Before T079, add initially failing fresh-head assertions in `tests/integration/test_schema_indexes.py` that every index named by the data model exists and that the composite-match index uses the exact normalized expressions and `COLLATE "C"` required by FR-016b / FR-016c; upgrade-path coverage remains owned by T079 (missing)
- [ ] T086 Add an integration test with one deliberately failing predicate that proves the run records `ERRORED`, continues remaining rules, and closes `COMPLETED_WITH_ERRORS` per FR-014 / spec Edge Cases (missing)
- [ ] T087 Add seeded acceptance coverage for one record failing three independent rules and for an empty batch remaining distinguishable from a missing feed per US1/AC3 / spec Edge Cases (missing)
- [ ] T088 Normalize YAML/Pydantic shape and vocabulary failures into `RuleDefinitionError` with the source filename, and test CLI error rendering without a traceback per contract: rule-definition registration outcomes (partial)
- [ ] T089 Create `README.md` if it does not exist and document the verified Feature 1 rollback procedure there and in `specs/001-data-foundation/quickstart.md`: cover Alembic downgrade boundaries, the required database role, handling of running or failed rule runs, and backup or export requirements for append-only findings; execute downgrade and restoration to head in an isolated prefixed test schema and record the verification evidence, with T072 later extending the README without replacing this material, per Constitution Development Workflow & Quality Gates (missing)
