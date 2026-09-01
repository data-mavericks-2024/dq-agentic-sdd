# Phase 0 Research — Deterministic Data-Quality Foundation

Decisions, rationale, and rejected alternatives for `001-data-foundation`.

**Revision 2** — rewritten after an independent constitution review found five blocking
contradictions in revision 1. Where a decision changed, the superseded reasoning is stated rather
than deleted; understanding why the first answer was wrong is what stops it recurring.

---

## D1 — How a rule predicate is represented

**Decision.** A rule version stores a parameterised SQL `SELECT` returning one row per failing
subject, with a fixed four-column output contract. The engine wraps it in a single
`INSERT … SELECT … ON CONFLICT DO NOTHING`.

**Rationale.** One round trip per rule rather than per record. At nine rules over 1M transactions
that is nine statements, not a million — the difference between seconds and hours across the
Singapore link. Adding a rule means inserting a registry row, satisfying SC-007.

**Alternatives considered.** Row-by-row evaluation in Python (misses SC-008 by orders of magnitude,
unrecoverable by tuning). A declarative DSL compiled to SQL (more machinery, still needs a SQL
escape hatch for alignment-gap and volume-deviation families). Per-rule views (ties versioning to
DDL, so every threshold change becomes a migration — conflicts with SC-005 and SC-007).

**Unchanged in revision 2.** The architecture is right; revision 1's error was in the premises
underneath it, addressed in D2 and D3.

---

## D2 — Keeping stored predicates deterministic

**Decision.** A `BEFORE INSERT` trigger on `dq.rule_version` parses `predicate_sql` and raises
unless:

1. It parses as a single `SELECT` — no semicolons, no multiple statements, no CTE-with-DML
2. It projects exactly the four contract columns
3. Every function appearing in an **expression** has `pg_proc.provolatile = 'i'` (IMMUTABLE)
4. No function call appears in `FROM`
5. Table references are **unqualified** and appear in a table allowlist
6. Every `LIMIT`, `DISTINCT ON`, and ranking window function has an `ORDER BY` terminating in a
   unique key
7. Every `:name` parameter is declared, or is one of the engine-bound `:batch_id`, `:as_of_date`,
   `:reference_watermark`

**What revision 1 got wrong, and why it matters.** Revision 1 used a *denylist* of six function
names — `random()`, `now()`, `current_timestamp`, `clock_timestamp()`, `gen_random_uuid()`,
`timeofday()` — checked in Python at registration time. Two independent failures:

- **The denylist omitted `CURRENT_DATE`**, along with `LOCALTIMESTAMP`, `LOCALTIME`, `CURRENT_TIME`,
  `statement_timestamp()`, `transaction_timestamp()`, and single-argument `age()`. `CURRENT_DATE` is
  the first function a timeliness-rule author writes — "was this feed delivered more than N days
  ago". Note that a "reject VOLATILE functions" rule would *also* have missed it: `current_date` and
  `now()` are STABLE, not VOLATILE. Only `provolatile = 'i'` catches the class.
- **It ran only in application code, in front of a role that can bypass it.** `dq_author` holds
  `INSERT` on `rule_version`. A check that lives solely in the registration path is a convention,
  not a mechanism — and constitution principle I explicitly does not accept conventions.

An IMMUTABLE allowlist is complete by construction rather than incomplete by nature: it is a
whitelist over a closed set, so a hazard nobody thought of is rejected by default instead of
admitted by default.

**Alternatives considered.** Trusting code review (rejected — this is precisely the class of error
that survives review because it looks correct). Execution-time sandboxing (the failure mode is a
wrong verdict, not a destructive one; sandboxing addresses the wrong risk).

---

## D3 — Reproducibility: pinning the reference world

**Decision.** Three things are bound into every run and recorded on `rule_run`:

| Bound value | Pins |
|---|---|
| `as_of_date` | The **business** world — which master-data version applies |
| `reference_watermark` | The **delivered** world — the max `batch_id` visible at run start |
| `session_settings` | The **execution** world — `TimeZone`, `DateStyle`, `search_path`, parallelism |

Predicates resolve master data using the as-of idiom in data-model.md, filtered by both
`:as_of_date` and `:reference_watermark`.

**What revision 1 got wrong.** D3 previously argued: *"Because a batch is immutable and a rule
version is immutable, the predicate's result set over a given batch is fixed — so the constraint is
sufficient."* That premise is false. FR-017 (orphan reference), FR-018 (alignment coverage), FR-020
(UoM consistency), and FR-022 (volume deviation) all join the subject batch to master data that was
**not** batch-scoped and grows with every later delivery. So a historical re-run months later would
see a different reference world and produce a different finding set — while every test passed,
because tests re-run within the same minute against the same reference world.

This was the most consequential defect in revision 1: the feature's headline promise (SC-003, User
Story 3, the whole inspection-defensibility argument) was false in production and invisible in test.

**Idempotency remains a database constraint.** `UNIQUE (rule_version_id, scope_key, subject_key)`
plus `ON CONFLICT DO NOTHING` guarantees non-duplication, and survives a run killed mid-flight. What
it never guaranteed — and revision 1 conflated — is *identity* of the finding set. Identity comes
from pinning the world, not from the constraint.

**Alternatives considered.** Batch-scoping master joins (eliminates the cross-source duplication
that FR-016a/b exist to detect). Snapshotting master data per run into a temp table (works, but
copies 100K+ rows per run for no benefit over a watermark filter).

### Recording the world is necessary but not sufficient — replay is the missing half

Verified 2026-08-26 by building T060, which could not be written as specified without an addition
nobody had noticed was needed.

Recording `as_of_date`, `reference_watermark`, and `session_settings` makes two runs *comparable*.
It does not make the second one reproduce the first. A fresh evaluation recomputes its context and
selects currently applicable rule versions, so once later data or rule versions exist it correctly
evaluates a different world. Replay must therefore be a distinct operation.

`run_rules(..., replay_of=<rule_run_id>)`, exposed as `dq run-rules --replay-of`, closes the gap. It
accepts only an original run with status `COMPLETED`; `RUNNING`, `FAILED`, and
`COMPLETED_WITH_ERRORS` runs are rejected as incomplete replay sources. Replay derives the original
scope, reuses its complete pinned context, and executes the exact `rule_version_id` set recorded in
`rule_run_rule_version`. Scope arguments and rule filters cannot be combined with replay.

Replay is recorded as a new append-only `rule_run` whose nullable `replay_of_rule_run_id` references
the original run. The existing finding uniqueness constraint remains unchanged, so a replay does
not duplicate finding rows. Equality is evaluated over rule version, scope key, subject key,
offending value, observed value, expected value, and severity; it does not require new findings to
be owned by the replay run. The replay's rule versions and outcomes are recorded in
`rule_run_rule_version`.

**Why the gap survived design review.** Both parameters were correctly identified, correctly stored,
and correctly bound into predicates. The missing piece was not a value but a verb, and reading the
schema does not reveal an absent operation. It surfaced the moment a test tried to *perform* the
guarantee rather than describe it.

### What T060 actually demonstrates

The test injects a **back-dated** master version: delivered in a later batch, stamped `valid_from`
inside the historical period. That shape is what separates the two parameters, and the test asserts
each half independently:

| Bound | Historical re-run sees the amendment? |
|---|---|
| `as_of_date` only | **Yes** — its business date falls inside the window |
| `as_of_date` + `reference_watermark` | No |

A design pinning only the business date would call the amended world a faithful reproduction. The
watermark is the only thing that excludes it, and the test fails loudly if it ever stops doing so.

The third assertion is the one that keeps the other two honest: the same predicate under a *fresh*
watermark must return something **different**. Without it, a test that pinned nothing would pass
identically and the whole mechanism would be unfalsifiable.

---

## D4 — Findings whose subject is not a record

**Decision.** `finding.subject_type` discriminates `record`, `batch`, and `source_period`.
`finding.batch_id` is **nullable**; a non-null `scope_key` carries the run's scope for every subject
type, and uniqueness is keyed on `scope_key` rather than `batch_id`.

**What revision 1 got wrong.** `batch_id` was `NOT NULL` and `run_rules(batch_id)` was batch-scoped
— but FR-021d and SC-009 require reporting a feed that *never arrived*, which by definition has no
batch. The quickstart papered over this by running `FEED-MISSING` against an unrelated batch. Worse,
`batch_id` sat in the uniqueness key, so the same missing period would re-fire as a fresh finding
against every subsequent batch, breaking FR-011 for the entire aggregate family.

`run_rules` therefore takes a scope, not a batch: either `batch=<id>` or
`source_period=<source, period>`.

---

## D5 — Test isolation on a single Supabase project

**Decision.** Schema names carry the prefix `test_<YYYYMMDDHHMMSS>_<uuid6>_`. A session creates its
five prefixed schemas, migrates into them, and drops them on teardown. Session start sweeps any
`test_*` schema whose **embedded timestamp** is older than four hours.

**Roles are never created or dropped by a prefixed run.** They are cluster-global, so schema
prefixing structurally cannot isolate them. A bootstrap migration creates the seven roles once, as
the Supabase superuser, and is skipped whenever `DQ_SCHEMA_PREFIX` is non-empty. Test sessions grant
the existing roles privileges on their own prefixed schemas only.

**What revision 1 got wrong, twice.**

- It said "drop any `test_*` schema whose creation timestamp is older than four hours".
  **PostgreSQL does not record schema creation time** — `pg_namespace` has no such column. The
  required compensating control was not implementable as written. Embedding the timestamp in the
  schema name fixes it with no extra table.
- It did not mention roles at all. A prefixed test run applying the same migration would either
  collide on `CREATE ROLE` or, if made idempotent, grant privileges on test schemas to the **shared
  development roles** — meaning the privilege test would be testing dev roles, and a teardown that
  dropped them would break development outright.

**Live-run protection.** Each pytest session keeps a dedicated session-pooler connection open and
holds a PostgreSQL session advisory lock derived deterministically from its canonical prefix. The
startup sweep first selects stale candidates by the timestamp embedded in the prefix, skips any
candidate whose lock is held, and drops an orphaned prefix only after acquiring its lock. Process
termination closes the dedicated connection and releases the lock, so liveness requires no
persistent registry. No test session creates or mutates a public session-registry table.

**Alternatives considered.** `testcontainers` (unavailable — no Docker; named because its absence is
the reason for all of this). A second Supabase project (recommended and declined; reversible by
repointing `TEST_DATABASE_URL`). A persistent public session registry (survives a crashed process
and violates prefix isolation unless separately governed). Transactional rollback per test (cannot
test migrations or DDL, and breaks on Alembic's advisory locks).

---

## D6 — Roles and connection modes

**Decision.** Alembic and every rule-run path use `SUPABASE_DB_STATEFUL_URL`, currently the session
pooler. The transaction pooler is not used anywhere in this feature.

Seven roles, all `NOINHERIT`, none a member of another: `dq_migrate`, `dq_ingest`, `dq_author`,
`dq_engine`, `dq_readonly`, `dq_sandbox`, `dq_publish`.

**What revision 1 got wrong — the governance, not the design.** Revision 1 added `dq_engine` as a
fifth role and argued in Complexity Tracking that this extended principle VI rather than weakening
it, on the grounds that *"it names four roles as the roles agents use, and the engine is not an
agent."* That reading does not survive contact with the principle's own text: its fourth item is
"migrations connect under a dedicated migration role", and migrations are not an agent. The list was
a list of capabilities requiring distinct roles, and the rule engine is such a capability.

The review then found **two further roles revision 1 silently needed and had not named**: seeding
writes `commercial`, which only `dq_publish` could do — the role constitutionally reserved for
post-approval publishing; and rule registration writes `dq.rule_version`, which only `dq_migrate`
could do — meaning principle I's enforcement ran in front of a connection that could bypass it.

So this was never a one-off deviation. It was a seven-role model wearing a four-role constitution.

**Resolution.** Constitution amended to **v1.1.0**: principle VI now names all seven roles, declares
the list exhaustive, and requires a conformance test asserting the database's `dq_*` roles equal
that list. An eighth role fails the build until the constitution is amended again. That converts the
precedent risk from a matter of each future author's restraint into a build failure.

---

## D7 — Effective-dated alignment boundaries

**Decision.** `[effective_from, effective_to)` — inclusive start, exclusive end — stored as a
`daterange`. **No exclusion constraint.**

**Rationale for half-open intervals.** The only convention under which consecutive periods neither
overlap by a day nor leave a phantom one-day gap — which is exactly the defect family FR-019 must
detect genuinely rather than spuriously.

**What revision 1 got wrong.** It added
`EXCLUDE USING gist (hcp_id WITH =, effective WITH &&)`, making overlapping alignment uninsertable,
and gestured at "staged source data can carry overlaps before it is curated" — but defined no
staging schema and no staging table. The overlap defect was therefore impossible to inject, so
SC-001's "every deliberately injected defect is detected" could not include it.

It also abandoned the rule this schema states two sections earlier: *constraints protect referential
truth; rules detect data-quality defects; where they conflict, the rule wins.* That rule was applied
correctly to `hcp.npi` and to `sales_transaction`'s text keys, then dropped here. Removing the
constraint restores consistency. The gap half of FR-019 was never affected.

---

## D8 — Meeting SC-008 without shipping a million rows over the wire

**Decision.** The volume test generates its dataset server-side with `generate_series`, marked
`@pytest.mark.volume`, excluded from the default suite, and dropping its data on teardown.

**Rationale.** Inserting 1M rows from Windows to Singapore would take far longer than the budget
being measured and would tell you nothing about rule-run performance.

**Size it before writing it.** 1M `sales_transaction` rows with four indexes, plus 100K versioned
HCP rows with three, plausibly consumes 250–350 MB of a 500 MB free-tier ceiling before findings and
before any orphaned test schema. The volume test may be **infeasible** rather than merely slow —
establish the storage arithmetic as the first task, not after building it.

### Measured (T007, 2026-08-24) — the volume test fits

Measured, not estimated, by `scripts/measure_storage.py`: a scratch schema built from the real
`dq.domain` table and index definitions, filled server-side with 20,000 rows of each shape, then
`pg_total_relation_size` extrapolated to the SC-008 target.

| Table | bytes/row total | heap | index | Target rows | Projected |
|---|---|---|---|---|---|
| `sales_transaction` | 206.4 | 102.0 | 104.4 | 1,000,000 | 196.9 MB |
| `hcp` | 338.3 | 111.0 | 227.3 | 100,000 | 32.3 MB |
| | | | | **Total** | **229.1 MB** |

Against a 500 MB ceiling that leaves **271 MB of headroom**, so T052 is buildable as designed and
the R1 storage risk is closed. The compute half of R1 is untouched by this and is still open until
T052 actually runs.

### T052 measured — both halves of R1 are now closed

Run on 2026-08-25 against the free-tier project, 1M `sales_transaction` rows and 100K versioned
`hcp` rows generated server-side:

| Measure | Result | Budget | Margin |
|---|---|---|---|
| Full rule run, seven record-level rules | **22.3 s** | 600 s (SC-008) | **27× under** |
| Dataset on disk, with indexes | **199.2 MB** | 500 MB | 301 MB spare |

The projection above over-estimated by 15% (229 MB predicted, 199 MB actual), which is the right
direction to be wrong in.

**22 seconds against a ten-minute budget is not a near miss, and the margin is the finding.** It
says the set-based decision in D1 was not merely sufficient but decisive: nine statements over a
million rows costs the same order of magnitude as nine statements over a thousand. `t4g.nano` was
the stated risk and it is not close to being the constraint — which also means the 10-minute figure
in SC-008 could be tightened substantially if a later feature wants a stricter guarantee, rather
than being a target to defend.

Two caveats, so the number is not read as more than it is. The volume dataset is deliberately clean,
so all seven rules returned zero findings — the timing measures predicate evaluation and not the
insert path, which is where a defect-heavy batch would spend its time. And the two aggregate rules
are excluded, because a batch scope does not evaluate them; a `source_period` run over a million
rows has not been timed.

**Indexes dominate, and that is the number worth remembering.** They are 51% of
`sales_transaction`'s footprint and 67% of `hcp`'s — the four-column `COLLATE "C"` composite-match
index costs more than the rows it indexes. Two consequences: an estimate derived from column widths
alone would have been out by a factor of three and would have called this infeasible; and the
cheapest lever, if a later feature does run short of space, is index selection rather than row
count.

The projection excludes findings, WAL, and any orphaned test schema. A full run over the volume
dataset produces findings in the low tens of thousands, well inside the headroom, but the sweep
(T028) still matters — three abandoned volume schemas would exhaust it.

---

## D9 — Master-data versioning *(new in revision 2)*

**Decision.** Master records are append-only version chains: natural key
`(source_system_id, source_key)`, a `valid_from date`, an `is_deleted` flag, and
`UNIQUE (source_system_id, source_key, valid_from)`. No `valid_to`. The version in effect on a date
is the one with the greatest `valid_from` not after it, resolved by the `ORDER BY … LIMIT 1` idiom
in data-model.md.

**Rationale.** One change fixes three separate defects:

1. **Duplicate detection.** Revision 1's master tables carried only `batch_id`, so a monthly
   re-delivery landed a second complete copy of every record — and FR-016a/FR-016b would have
   flagged the entire master file as duplicates of its own prior delivery. Version chains make
   "the same record, later" distinct from "two records, same identity".
2. **Reproducibility.** As-of resolution is what lets D3 pin the reference world.
3. **Feature 6's publish collision.** `dq_publish` needs to correct curated data, but FR-003b makes
   batch contents immutable. With version chains a correction is a *new version* with a later
   `valid_from`, not an `UPDATE` — so the publish path and batch immutability stop contradicting
   each other. Revision 1 left this unresolved and silently resolved in favour of "UPDATE is
   allowed", which under-enforced FR-003b from the start.

**Why no `valid_to`.** Storing an end date requires updating the previous row when a new version
arrives. A mutable master table reintroduces exactly the reproducibility problem this design
removes, and would need the immutability trigger to carve out an exception — an exception that then
has to be trusted.

**Alternatives considered.** An `is_current` boolean (simplest, but mutable — a historical re-run
sees a different world, so SC-003 stays broken and the Feature 6 collision survives). A
latest-batch-per-source view (same mutability problem, no compensating benefit). Batch-scoped
duplicate detection only (blind to the cross-source duplication the spec names as the primary case).

**Cost accepted.** The loader must stamp `valid_from` and the seed generator must produce versioned
master data across several periods. Roughly a day of extra work in Feature 1, against a schema
migration on populated tables later.

---

## D10 — Pinning the execution environment *(new in revision 2)*

**Decision.** Every engine connection issues, before any predicate runs:

```sql
SET LOCAL TimeZone = 'UTC';
SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL search_path = <prefix>commercial, <prefix>dq;
SET LOCAL statement_timeout = '15min';
```

All four are recorded in `rule_run.session_settings`.

**Rationale.** Three distinct hazards, none addressed in revision 1:

- **Timezone.** `data_batch.arrival_ts` is `timestamptz` while `business_period` and `txn_date` are
  date-typed, so FR-021b's late/missing logic necessarily crosses that boundary — and every
  `timestamptz → date` cast resolves against the session `TimeZone` GUC. A feed "late" in UTC is
  "on time" in `Asia/Singapore`. The CLI, pytest, and the pooler may each default differently.
- **`search_path`.** Predicates use unqualified table names so the schema prefix can resolve
  (D2 rule 5). That makes resolution session-dependent unless the engine pins it — and pinning it
  explicitly is what makes the prefix mechanism deterministic rather than a hazard.
- **`statement_timeout`.** A rule running near the limit yields `ERRORED` on one run and `EVALUATED`
  on another, producing different finding sets from identical inputs.

---

## D11 — How the determinism test must actually work *(new in revision 2)*

**Decision.** The determinism check executes `predicate_sql` **twice into two result sets and
compares full rows**, including `offending_value`. It varies
`max_parallel_workers_per_gather` (0 and 4) and `enable_indexscan` (on and off) between executions.
It does **not** compare persisted findings.

**Why revision 1's version could never work.** Revision 1's R3 proposed "run each rule twice against
a fixed batch and assert set equality" — observing *persisted findings*. But
`ON CONFLICT DO NOTHING` silently discards a second run's differing row for the same
`subject_key`. So the persisted set is byte-identical across runs **by construction, not by
determinism**. The idempotency mechanism guarantees the test passes; the non-determinism it exists
to catch is precisely what the mechanism hides.

This is not exotic. Master-data joins can fan out, producing multiple rows per subject differing in
`offending_value`, with plan order deciding which survives.

**Why plan variation matters.** Collation-order effects, tie-broken window functions, and
aggregation-order effects only surface when the query plan differs. Two identical executions
seconds apart certify them deterministic.

---

## Constitution enforcement — structural mechanisms

Required by the constitution's Development Workflow section. Verified against **v1.1.0**.

| Principle | Mechanism | Where |
|---|---|---|
| I — Determinism First | `BEFORE INSERT` trigger on `rule_version` requiring `provolatile = 'i'` for every expression function, in the database rather than only in the registration path | D2 |
| II — No Autonomous Writes | `dq_engine` holds no write grant on `commercial`; a negative test attempts each write and asserts refusal; `run_rules` asserts `current_user` at connection open | D6, data-model.md |
| V — Full Auditability | `rule_version` immutable; `finding` immutable by trigger; `correlation_id` on `rule_run`; `as_of_date` + `reference_watermark` + `session_settings` record the world each run saw | D3, data-model.md |
| VI — Least-Privilege | Seven `NOINHERIT` roles, explicit grant matrix, no cross-membership, conformance test asserting the role set equals the constitution's list | D6 |
| IX — Resumability (idempotence) | `UNIQUE (rule_version_id, scope_key, subject_key)` + `ON CONFLICT DO NOTHING`, uniform across subject types | D3, D4 |
| X — Testability | Expected finding set derived from the generator, not hand-declared; set-equality assertion; determinism test comparing predicate output under varied plans | D11, quickstart.md |

**Still not enforced structurally, and stated plainly.** Principle VII (PII/PHI) holds only because
the data is synthetic and no prompt boundary exists. There is no masking mechanism because there is
nothing to mask into. This must be built at Feature 3, where commercial data first enters a model
prompt. Nothing in the database marks it synthetic-only, so `spec.md` § Assumptions is currently the
only thing standing between Feature 2 and real HCP data.

---

## Three largest technical risks

**R1 — Free-tier capacity, on both axes.** `t4g.nano` compute may not complete a 1M-row rule run
inside 10 minutes regardless of SQL quality, and 500 MB storage may not hold the volume dataset plus
indexes plus findings plus an orphaned test schema. *Would become real if:* the volume test fails or
cannot be provisioned on a design that is otherwise correct. *Mitigation:* establish the storage
arithmetic before writing the test, and run the test as an early task rather than a late one. If it
fails, the options are indexing work, a lower stated target, or paid compute — all cheaper to face
now than at Feature 5.

**R2 — The as-of join idiom is easy to write incorrectly.** Every predicate touching master data
must apply both `:as_of_date` and `:reference_watermark`. A predicate omitting the watermark still
returns plausible results and still passes the determinism test (which runs twice in the same
minute), but silently breaks historical reproducibility — the exact failure that made revision 1
wrong. *Would become real if:* a rule author copies a simpler join. *Mitigation:* the D2 trigger
should additionally reject any predicate referencing a master table without binding both parameters.
Structural, and it makes the class impossible rather than discouraged.

**R3 — Set-based execution cannot report per-record evaluation errors.** `spec.md` § Edge Cases
requires recording a rule as errored "for that record", but one malformed value aborts the whole
statement and the rule contributes zero findings for the entire batch — including records that would
legitimately have failed. `rule_run_rule_version.outcome` records `ERRORED` at rule granularity
only. *Would become real if:* a steward reads a batch summary as clean when a rule silently
contributed nothing. *Mitigation:* the spec's edge case is amended to per-rule granularity (the
honest description of what set-based execution can do), `rule_run.status` becomes
`COMPLETED_WITH_ERRORS`, and `summarise_batch` surfaces errored rules as a first-class line rather
than an absence.
