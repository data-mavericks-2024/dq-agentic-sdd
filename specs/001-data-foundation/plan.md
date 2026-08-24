# Implementation Plan: Deterministic Data-Quality Foundation

**Branch**: `001-data-foundation` | **Date**: 2026-08-24 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-data-foundation/spec.md`

## Summary

Build the commercial data model, a versioned rule registry, and a deterministic rule runner that
evaluates SQL predicates set-wise against an immutable data batch and persists one finding per
failure. No agents, no model calls, no HTTP API, no user interface.

The central technical decision is that **a rule is a stored, versioned, parameterised SQL `SELECT`
that returns failing subjects**, and the engine wraps it in a single `INSERT … SELECT … ON CONFLICT
DO NOTHING`. This yields set-based evaluation (one round trip per rule rather than per record),
which is what makes the 10-minute target at 1M transactions achievable over a network link to
Singapore, and it makes idempotency a property of a database constraint rather than of application
logic.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: SQLAlchemy Core (schema definition and query construction), Alembic
(migrations), psycopg 3 (driver), Pydantic v2 (rule definition validation), pytest (tests),
ruff + mypy strict (lint and types). No LangGraph, no `anthropic` SDK, no FastAPI, no Streamlit —
all four are out of scope for this feature.

**Storage**: Supabase-hosted PostgreSQL 17.6, `ap-southeast-1`. Single project. Reached over the
**session pooler** (`aws-0-ap-southeast-1.pooler.supabase.com:5432`) because the development network
is IPv4-only and the direct endpoint is IPv6-only on free tier. Prepared statements and advisory
locks verified working on this endpoint.

**Testing**: pytest. Integration tests run against the same Supabase project as development, isolated
by a per-session schema prefix (`test_<uuid>_`), with a session-start sweep for orphaned schemas.
`testcontainers` is unavailable — no Docker on the development machine.

**Target Platform**: Linux/Windows server process; developed on Windows 11.

**Project Type**: Single project — a library plus a thin operational entry point.

**Performance Goals**: A full rule run over one month of data at target scale completes in under
10 minutes (SC-008).

**Constraints**: Every database interaction crosses a network link to Singapore, so per-record round
trips are disproportionately expensive. Rule evaluation must be set-based. Free-tier database:
500 MB storage, `t4g.nano` compute, pauses after roughly a week of inactivity.

**Scale/Scope**: ~1M sales transactions per month, ~100K HCP master records, ~5K products, ~5K
territories. Nine rule families at launch; the registry must accept new rules without engine code
changes.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` **v1.1.0**.

> **Revision 2.** An independent review of revision 1 found five blocking contradictions and
> several claims of structural enforcement that were in fact conventions. The design artifacts were
> revised and the constitution amended to v1.1.0. Changes are itemised in data-model.md § Changes
> from revision 1 and in research.md, which states what each superseded decision got wrong rather
> than deleting it.

### Pre-Phase 0 evaluation

| # | Principle | Applies to Feature 1? | Gate status |
|---|---|---|---|
| I | Determinism First | **Yes — this feature *is* the deterministic layer** | PASS by construction: no model is involved anywhere in this feature |
| II | No Autonomous Writes | Partially — no agents exist yet | PASS: the engine role cannot write to `commercial` |
| III | Evidence-Bound Reasoning | No — no reasoning in this feature | N/A |
| IV | Simulate Before Propose | No — no corrections proposed | N/A, but the `sandbox` schema is created here for later use |
| V | Full Auditability | Partially — rule runs must be reconstructable | PASS: `rule_run` + immutable `rule_version` |
| VI | Least-Privilege Data Access | **Yes — the roles originate here** | PASS: seven roles per v1.1.0, plus a conformance test pinning the set |
| VII | PII/PHI Discipline | Partially — no prompts, but HCP data exists | PASS: synthetic data only, no prompt boundary exists |
| VIII | Deterministic Orchestration | No — no graph in this feature | N/A |
| IX | Resumability | Partially — runs must be re-runnable | PASS: idempotent re-run via unique constraint |
| X | Testability | **Yes** | PASS: rules independently testable; seeded defect set |
| XI | UI Is Not a Trust Boundary | No — no UI, no API | N/A |

### Principle VI — resolved by amendment, not by deviation

Revision 1 added a fifth role, `dq_engine`, and argued it extended principle VI. That argument did
not survive review: principle VI's four-item list included *migrations*, which is not an agent, so
"those four are the agent roles" was a misreading. The review also found **two further roles
revision 1 silently needed** — `dq_ingest` (seeding writes `commercial`, which only `dq_publish`
could) and `dq_author` (rule registration writes `dq.rule_version`, which only `dq_migrate` could,
meaning principle I's enforcement ran in front of a connection that could bypass it).

So this was never a one-off deviation; it was a seven-role model wearing a four-role constitution.

**Resolution:** the constitution was amended to **v1.1.0** — principle VI now names all seven roles,
declares the list exhaustive, and requires a conformance test asserting the database's `dq_*` roles
equal that list. An eighth role fails the build until the constitution is amended again. This is no
longer a deviation and no longer appears in Complexity Tracking. Grant matrix in
[data-model.md](./data-model.md).

### Post-Phase 1 re-evaluation

See [research.md](./research.md) § Constitution enforcement for the mechanism behind each verdict.

| # | Principle | Enforcing design element | Verdict |
|---|---|---|---|
| I | Determinism First | `BEFORE INSERT` trigger on `rule_version` requiring `provolatile = 'i'` for every expression function — **in the database**, because `dq_author` can insert directly. Replaces revision 1's denylist, which omitted `CURRENT_DATE` and ran only in application code | **Enforced structurally** |
| II | No Autonomous Writes | `dq_engine` holds no write grant on `commercial`; a negative test attempts each write and asserts refusal; `run_rules` asserts `current_user` at connection open so a mis-pointed URL cannot void it silently | **Enforced structurally** |
| V | Full Auditability | `rule_version` and `finding` immutable by trigger; `correlation_id` on `rule_run`; `as_of_date` + `reference_watermark` + `session_settings` record the world each run saw | **Enforced structurally** |
| VI | Least-Privilege | Seven `NOINHERIT` roles, explicit grant matrix, no cross-membership, conformance test pinning the role set to the constitution's list | **Enforced structurally** |
| IX | Resumability — idempotence | `UNIQUE (rule_version_id, scope_key, subject_key)` + `ON CONFLICT DO NOTHING`, uniform across all three subject types | **Enforced structurally** |
| IX | Resumability — reproducibility | `as_of_date` + `reference_watermark` pin the reference world and are recorded per run. Revision 1 claimed this followed from batch immutability alone, which was false: four rule families join unbounded master data | **Enforced structurally** |
| X | Testability | Expected finding set derived from the generator rather than hand-declared; set-equality assertion; determinism test comparing **predicate output** under varied query plans, not persisted findings | **Enforced structurally** |
| VII | PII/PHI | Synthetic data only; no prompt boundary exists. No masking mechanism exists because there is nothing to mask into | **Not enforced — deferred to Feature 3**, stated plainly rather than claimed |

## Project Structure

### Documentation (this feature)

```text
specs/001-data-foundation/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── rule-definition.md
│   └── rule-runner.md
├── checklists/
│   └── requirements.md
├── spec.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
src/dq/
├── config/
│   ├── settings.py          # env loading, connection resolution, schema prefix
│   └── models.py            # model IDs — placeholder, unused until Feature 3
├── db/
│   ├── engine.py            # connection factory per role
│   ├── schemas.py           # schema names, prefix resolution
│   └── types.py             # shared column types
├── domain/
│   ├── commercial.py        # SQLAlchemy Core tables: hcp, hco, product, territory,
│   │                        #   territory_alignment, sales_transaction, data_batch, source_system
│   └── dq.py                # rule, rule_version, feed_expectation, rule_run, finding
├── rules/
│   ├── definition.py        # Pydantic RuleDefinition + validation
│   ├── registry.py          # register / version / activate / deactivate
│   ├── predicates.py        # predicate safety validation
│   └── library/             # the nine rule families as declarative definitions
├── engine/
│   ├── runner.py            # executes active rule versions against a batch
│   └── summary.py           # per-batch aggregation
├── seed/
│   ├── generator.py         # synthetic commercial data
│   └── defects.py           # deliberate defect injection + expected finding set
└── cli.py                   # operational entry point: seed, run-rules, summarise

migrations/
├── env.py                   # schema-prefix aware
└── versions/

tests/
├── unit/                    # rule definition validation, predicate safety, summary math
├── integration/             # against Supabase, schema-isolated
├── volume/                  # marked; 1M-row performance check (SC-008)
└── conftest.py              # schema lifecycle + stale-schema sweep
```

**Structure Decision**: Single project. Feature 1 produces a library (`src/dq/`) plus a thin CLI
used to trigger runs and seed data. The CLI is an operational entry point, not a user interface —
it issues no interactive workflow and makes no trust decision, so it does not engage principle XI.
FastAPI and Streamlit directories are deliberately absent; they arrive in Feature 6.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Rule predicates stored as parameterised SQL rather than a declarative DSL | Set-based evaluation is required to meet SC-008 at 1M rows over a network link; SQL expresses this natively | A declarative DSL compiling to SQL is substantially more machinery for the same result, and would still need a SQL escape hatch for the alignment-gap and volume-deviation families. Row-by-row evaluation in Python was rejected outright: at 1M rows over a Singapore link it misses the 10-minute target by orders of magnitude |
| Master data modelled as append-only version chains rather than current-state tables | Fixes three defects at once: duplicate rules flagging re-delivered master records against their own prior copy; reproducibility being false because predicates joined unbounded master data; and Feature 6's governed publish colliding with batch immutability | An `is_current` flag or a latest-batch-per-source view is simpler but *mutable*, so a historical re-run sees a different world — SC-003 stays broken and the Feature 6 collision survives. Batch-scoped duplicate detection is simplest of all and blind to the cross-source duplication the spec names as the primary case |
| Schema-prefix parameterisation in Alembic, with role creation split into a separate bootstrap migration | One Supabase project serves both development and tests, so isolation must be by schema — but roles are cluster-global and cannot be schema-isolated at all | A separate test project was declined (Session 2). Without prefixing, tests share dev schemas. Without splitting role creation out, a prefixed test run either collides on `CREATE ROLE` or grants privileges on test schemas to the shared development roles |
| Three parameters (`as_of_date`, `reference_watermark`, `session_settings`) bound and recorded per run | Reproducibility (SC-003) requires pinning the business world, the delivered world, and the execution environment. Any one left unpinned makes a historical re-run diverge silently | Relying on batch and rule-version immutability alone was revision 1's approach and was demonstrably wrong — four rule families join master data that is not batch-scoped |
