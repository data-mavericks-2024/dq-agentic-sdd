# Data Model — Deterministic Data-Quality Foundation

PostgreSQL 17.6 on Supabase. All identifiers lower-case snake_case.

**Revision 2** — rewritten after the independent constitution review. Changes from revision 1 are
summarised at the foot of this document.

Schema names carry the prefix `${P}`, empty in development and
`test_<YYYYMMDDHHMMSS>_<uuid6>_` under test (research.md D5).

## Schemas

| Schema | Holds | Created in |
|---|---|---|
| `${P}commercial` | Curated commercial data | Feature 1 |
| `${P}dq` | Rule registry, runs, findings, feed expectations | Feature 1 |
| `${P}workflow` | Agent/workflow state (LangGraph checkpoints) | Feature 1 (empty) |
| `${P}audit` | Append-only audit trail | Feature 1 (empty) |
| `${P}sandbox` | Remediation simulation | Feature 1 (empty) |

---

## Roles and grants

Seven roles, all `NOINHERIT`, none granted membership in another. Constitution principle VI
**v1.1.0**, where this list is exhaustive.

**Roles are cluster-global and therefore cannot be schema-isolated.** They are created once by a
dedicated bootstrap migration that runs as the Supabase `postgres` superuser and is skipped when a
schema prefix is set. Test sessions never create or drop roles; they grant the existing roles
privileges on their own prefixed schemas only. Getting this wrong means a test run mutates
development privileges.

```sql
-- Bootstrap migration only. Skipped when DQ_SCHEMA_PREFIX is non-empty.
CREATE ROLE dq_migrate  LOGIN NOINHERIT PASSWORD :'dq_migrate_pw';
CREATE ROLE dq_ingest   LOGIN NOINHERIT PASSWORD :'dq_ingest_pw';
CREATE ROLE dq_author   LOGIN NOINHERIT PASSWORD :'dq_author_pw';
CREATE ROLE dq_engine   LOGIN NOINHERIT PASSWORD :'dq_engine_pw';
CREATE ROLE dq_readonly LOGIN NOINHERIT PASSWORD :'dq_readonly_pw';
CREATE ROLE dq_sandbox  LOGIN NOINHERIT PASSWORD :'dq_sandbox_pw';
CREATE ROLE dq_publish  LOGIN NOINHERIT PASSWORD :'dq_publish_pw';
```

Passwords are supplied as bound migration parameters from the environment, never literals in a
migration file. The bootstrap migration is the one migration that does not run as `dq_migrate` —
it creates that role.

### Grant matrix

| Role | `commercial` | `dq` | `sandbox` | `workflow` | `audit` |
|---|---|---|---|---|---|
| `dq_migrate` | OWNER (DDL) | OWNER | OWNER | OWNER | OWNER |
| `dq_ingest` | SELECT, INSERT | — | — | — | INSERT |
| `dq_author` | — | SELECT; INSERT on `rule`, `rule_version`; UPDATE `rule.is_active` | — | — | INSERT |
| `dq_engine` | **SELECT only** | SELECT; INSERT on `finding`, `rule_run`, `rule_run_rule_version` | — | — | INSERT |
| `dq_readonly` | SELECT | SELECT | SELECT | *see note* | *see note* |
| `dq_sandbox` | SELECT | SELECT | ALL | — | INSERT |
| `dq_publish` | SELECT, INSERT | SELECT | — | — | INSERT |

```sql
-- dq_engine: reads commercial, writes findings. Never writes commercial.
GRANT USAGE ON SCHEMA ${P}commercial, ${P}dq TO dq_engine;
GRANT SELECT ON ALL TABLES IN SCHEMA ${P}commercial, ${P}dq TO dq_engine;
GRANT INSERT ON ${P}dq.finding, ${P}dq.rule_run, ${P}dq.rule_run_rule_version TO dq_engine;
GRANT UPDATE (finished_at, status, error_detail) ON ${P}dq.rule_run TO dq_engine;

-- dq_author: writes the rule registry. No access to commercial data at all.
GRANT USAGE ON SCHEMA ${P}dq TO dq_author;
GRANT SELECT ON ALL TABLES IN SCHEMA ${P}dq TO dq_author;
GRANT INSERT ON ${P}dq.rule, ${P}dq.rule_version TO dq_author;
GRANT UPDATE (is_active) ON ${P}dq.rule TO dq_author;

-- dq_ingest: loads source data. Cannot read quality metadata.
GRANT USAGE ON SCHEMA ${P}commercial TO dq_ingest;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA ${P}commercial TO dq_ingest;

-- Defaults so tables added by later migrations inherit the same shape, per schema and per verb.
ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA ${P}commercial
  GRANT SELECT ON TABLES TO dq_engine, dq_readonly, dq_sandbox, dq_publish, dq_ingest;
ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA ${P}dq
  GRANT SELECT ON TABLES TO dq_engine, dq_readonly, dq_sandbox, dq_author;
ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA ${P}sandbox
  GRANT SELECT ON TABLES TO dq_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE dq_migrate IN SCHEMA ${P}audit
  GRANT INSERT ON TABLES TO dq_ingest, dq_author, dq_engine, dq_sandbox, dq_publish;
```

**`dq_readonly` on `audit` and `workflow` — deferred, deliberately.** Those schemas will hold
persisted prompt payloads and checkpointed agent state, and `dq_readonly` is the role investigation
agents connect under. An agent processing an injected instruction inside a data value could read
other investigations' payloads. Both schemas are empty in this feature, so no grant is issued and
the decision is deferred to Feature 3 where it becomes real. Recorded here so it is not mistaken for
an oversight.

### Required conformance test — constitution VI

```sql
SELECT rolname FROM pg_roles WHERE rolname LIKE 'dq\_%' ORDER BY 1;
```

Must equal exactly: `dq_author, dq_engine, dq_ingest, dq_migrate, dq_publish, dq_readonly,
dq_sandbox`. An eighth role fails the build until the constitution is amended. This is the
mechanism that keeps least privilege from eroding by accumulation.

### Required privilege test

Connect as `dq_engine` and assert every one of these raises `InsufficientPrivilege`:
`INSERT INTO commercial.sales_transaction`, `UPDATE commercial.hcp`,
`DELETE FROM commercial.product`, `CREATE TABLE commercial.scratch`.
Also assert `dq_author` cannot `SELECT` from any `commercial` table.

The test must **attempt** each write. A test that passes because the code never tries is worthless.

`run_rules` additionally asserts `SELECT current_user = 'dq_engine'` at connection open — otherwise
pointing `DQ_ENGINE_URL` at `dq_migrate` silently voids the guarantee with no error.

---

## `commercial` schema

### Master-data versioning — the central modelling decision

Master records (`hcp`, `hco`, `product`, `territory`) are **append-only version chains**, not
current-state tables. Each row is one version of one natural key, stamped with the date from which
it applies. Nothing is ever updated or deleted.

| Concept | Column |
|---|---|
| Natural key | `(source_system_id, source_key)` |
| Version stamp | `valid_from date NOT NULL` |
| Delivering batch | `batch_id bigint NOT NULL` |
| Soft delete | `is_deleted boolean NOT NULL DEFAULT false` |

`UNIQUE (source_system_id, source_key, valid_from)` on every master table. This uniqueness is what
makes as-of resolution deterministic — without it, "the version in effect on date D" has no
single answer.

**There is no `valid_to` column.** The version in effect on a date is the one with the greatest
`valid_from` not after it. Storing an end date would require updating the previous row when a new
version arrives, and a mutable master table would reintroduce exactly the reproducibility problem
this design exists to remove.

**As-of resolution idiom.** Every predicate that joins master data uses this shape, with
`:as_of_date` and `:reference_watermark` bound by the engine:

```sql
JOIN LATERAL (
  SELECT p2.*
  FROM   product p2
  WHERE  p2.source_system_id = t.source_system_id
    AND  p2.source_key       = t.product_key
    AND  p2.valid_from      <= :as_of_date
    AND  p2.batch_id        <= :reference_watermark
  ORDER BY p2.valid_from DESC
  LIMIT  1
) p ON NOT p.is_deleted
```

`ORDER BY … LIMIT 1` terminates in `valid_from`, unique per natural key — so the tie-break is total
and the result is deterministic.

**Why the watermark as well as the date.** `as_of_date` pins the *business* world; the watermark
pins the *delivered* world. Without it, a master version delivered later but back-dated to an
earlier `valid_from` would change the outcome of a historical re-run. With both bound and recorded
on `rule_run`, a re-run reproduces exactly. This is what makes SC-003 true rather than merely
claimed.

### `source_system`

| Column | Type | Notes |
|---|---|---|
| `source_system_id` | `smallint` PK | |
| `code` | `text` UNIQUE NOT NULL | e.g. `VEEVA`, `IQVIA_DDD` |
| `name` | `text` NOT NULL | |

### `data_batch`

One physical arrival. Immutable once recorded (FR-003b), enforced by a trigger raising on `UPDATE`
or `DELETE` — not by convention.

| Column | Type | Notes |
|---|---|---|
| `batch_id` | `bigint` PK | |
| `source_system_id` | `smallint` FK NOT NULL | |
| `arrival_ts` | `timestamptz` NOT NULL | |
| `business_period` | `daterange` NOT NULL | the period the data describes |
| `record_count` | `integer` NOT NULL | `CHECK (record_count >= 0)` — zero is legal and distinct from absent |
| `sealed_at` | `timestamptz` NOT NULL DEFAULT `now()` | |

`UNIQUE (source_system_id, arrival_ts)`.

The same trigger protects member rows: `sales_transaction`, `hcp`, `hco`, `product`, `territory`,
and `territory_alignment` reject `UPDATE` and `DELETE` outright. Revision 1 protected only the batch
header, which left FR-003b enforced on the container and not its contents.

### `hcp`

| Column | Type | Notes |
|---|---|---|
| `hcp_id` | `bigint` PK | surrogate key for this *version* |
| `source_system_id` | `smallint` FK NOT NULL | |
| `source_key` | `text` NOT NULL | natural key in the source system |
| `valid_from` | `date` NOT NULL | |
| `batch_id` | `bigint` FK NOT NULL | |
| `is_deleted` | `boolean` NOT NULL DEFAULT false | |
| `npi` | `text` NULL | nullable, unvalidated — see below |
| `first_name`, `last_name` | `text` | |
| `postal_code` | `text` NULL | |
| `licence_state` | `text` NULL | |
| `hco_source_key` | `text` NULL | affiliation, by natural key |

`UNIQUE (source_system_id, source_key, valid_from)`.

**`npi` is deliberately nullable and unconstrained.** Rejecting a malformed NPI at insert would
prevent the engine from ever detecting one. General rule for this schema: *constraints protect
referential truth; rules detect data-quality defects. Where they conflict, the rule wins.*

Indexes: `(source_system_id, source_key, valid_from DESC)` for as-of resolution;
`(npi) WHERE npi IS NOT NULL` for FR-016a;
`(last_name COLLATE "C", left(first_name,1) COLLATE "C", postal_code COLLATE "C", licence_state COLLATE "C")`
for FR-016b.

**`COLLATE "C"` is explicit and load-bearing.** A managed-platform glibc or ICU upgrade changes text
sort order under a locale-dependent collation, leaving existing indexes inconsistent — so an index
scan and a sequential scan of the same predicate can return different rows. `"C"` is byte-ordered,
immune to that, and faster. Under a non-deterministic ICU collation, text `=` would also become
case- and accent-insensitive, silently redefining what FR-016b matches without a new rule version.

### `hco`, `product`, `territory`

Same versioning columns as `hcp` — `(source_system_id, source_key, valid_from, batch_id,
is_deleted)` plus `UNIQUE (source_system_id, source_key, valid_from)`.

`product` additionally: `name`, `uom` (canonical unit of measure), `status` (`ACTIVE`/`RETIRED`),
`retired_on date NULL`.

`territory` additionally: `code`.

### `territory_alignment`

Effective-dated HCP-to-territory assignment. Half-open `[from, to)` interval (research.md D7).

| Column | Type | Notes |
|---|---|---|
| `alignment_id` | `bigint` PK | |
| `batch_id` | `bigint` FK NOT NULL | |
| `source_system_id` | `smallint` FK NOT NULL | |
| `hcp_source_key` | `text` NOT NULL | |
| `territory_code` | `text` NOT NULL | |
| `effective` | `daterange` NOT NULL | `[from, to)` |

**No exclusion constraint.** Revision 1 carried
`EXCLUDE USING gist (hcp_id WITH =, effective WITH &&)`, which made overlapping alignment
uninsertable — and therefore made FR-019's overlap defect impossible to inject, so SC-001 could not
cover it. That contradicted this schema's own stated rule. Overlap is a data-quality defect, so the
rule detects it and no constraint prevents it landing.

Index: `(source_system_id, hcp_source_key, effective)` using GiST for range containment.

### `sales_transaction`

| Column | Type | Notes |
|---|---|---|
| `txn_id` | `bigint` PK | |
| `batch_id` | `bigint` FK NOT NULL | |
| `source_system_id` | `smallint` FK NOT NULL | |
| `source_txn_key` | `text` NOT NULL | |
| `product_key` | `text` NOT NULL | **text, not FK** — an orphan reference is a defect (FR-017) |
| `hcp_key` | `text` NOT NULL | **text, not FK** — same reason |
| `territory_code` | `text` NOT NULL | |
| `txn_date` | `date` NOT NULL | |
| `quantity` | `numeric(18,4)` NOT NULL | exact; `sum`/`avg` are order-independent |
| `uom` | `text` NOT NULL | |

Master-data references are stored as source keys, not foreign keys, for the same reason `npi` is
unconstrained: a foreign key would reject the orphaned transaction at insert, making FR-017
permanently undetectable.

Indexes: `(txn_date)`, `(source_system_id, product_key)`, `(source_system_id, hcp_key)`, `(batch_id)`.

---

## `dq` schema

### `rule`

| Column | Type | Notes |
|---|---|---|
| `rule_id` | `integer` PK | |
| `rule_key` | `text` UNIQUE NOT NULL | e.g. `HCP-NPI-FORMAT` |
| `domain` | `text` NOT NULL | `CHECK` constrained |
| `dimension` | `text` NOT NULL | the seven quality dimensions; `CHECK` constrained |
| `owning_function` | `text` NOT NULL | |
| `is_active` | `boolean` NOT NULL DEFAULT true | |

All four declarations `NOT NULL` — FR-005 enforced by the schema, not by validation code that can be
forgotten.

### `rule_version`

Immutable. No role but `dq_migrate` holds `UPDATE` or `DELETE`.

| Column | Type | Notes |
|---|---|---|
| `rule_version_id` | `bigint` PK | |
| `rule_id` | `integer` FK NOT NULL | |
| `version_no` | `integer` NOT NULL | |
| `severity` | `text` NOT NULL | `HIGH`/`MEDIUM`/`LOW` — versioned, because severity changes meaning |
| `subject_type` | `text` NOT NULL | `record` / `batch` / `source_period` |
| `predicate_sql` | `text` NOT NULL | |
| `parameters` | `jsonb` NOT NULL DEFAULT `'{}'` | |
| `created_at` | `timestamptz` NOT NULL | |

`UNIQUE (rule_id, version_no)`.

**`BEFORE INSERT` trigger enforcing predicate determinism.** The trigger parses `predicate_sql`,
resolves every function referenced in an expression, and raises unless all have
`pg_proc.provolatile = 'i'` (IMMUTABLE). It also rejects schema-qualified table references, function
calls in `FROM`, and any `LIMIT` or ranking window whose `ORDER BY` does not terminate in a unique
key.

This is in the database, not only in the Python registration path, because `dq_author` can `INSERT`
here directly. Revision 1 put the check solely in application code sitting in front of a connection
that could bypass it — which meant principle I was enforced by convention, not structurally.

An IMMUTABLE allowlist supersedes revision 1's denylist of six function names. That denylist omitted
`CURRENT_DATE`, `LOCALTIMESTAMP`, `LOCALTIME`, `CURRENT_TIME`, `statement_timestamp()`,
`transaction_timestamp()`, and single-argument `age()`. `CURRENT_DATE` is the first function a
timeliness-rule author would reach for, and it is STABLE rather than VOLATILE — so a
"reject volatile" rule would also have missed it. `provolatile = 'i'` catches the whole class.

### `feed_expectation`

| Column | Type | Notes |
|---|---|---|
| `feed_expectation_id` | `integer` PK | |
| `source_system_id` | `smallint` FK NOT NULL | |
| `cadence` | `text` NOT NULL | `DAILY`/`WEEKLY`/`MONTHLY` |
| `delivery_window` | `interval` NOT NULL | grace period after period end before "late" |
| `active` | `daterange` NOT NULL | end-date to retire (FR-021c) |

### `rule_run`

| Column | Type | Notes |
|---|---|---|
| `rule_run_id` | `bigint` PK | |
| `replay_of_rule_run_id` | `bigint` FK NULL | self-reference to the original `COMPLETED` run; `ON DELETE RESTRICT` |
| `correlation_id` | `uuid` NOT NULL | constitution principle V; Features 2+ join on this |
| `scope_type` | `text` NOT NULL | `batch` / `source_period` |
| `batch_id` | `bigint` FK NULL | set when `scope_type = 'batch'` |
| `scope_source_system_id` | `smallint` FK NULL | set when `scope_type = 'source_period'` |
| `scope_period` | `daterange` NULL | set when `scope_type = 'source_period'` |
| `as_of_date` | `date` NOT NULL | bound into every predicate |
| `reference_watermark` | `bigint` NOT NULL | max visible `batch_id` at run start |
| `session_settings` | `jsonb` NOT NULL | pinned `TimeZone`, `DateStyle`, `search_path`, `max_parallel_workers_per_gather` |
| `started_at` | `timestamptz` NOT NULL | |
| `finished_at` | `timestamptz` NULL | |
| `status` | `text` NOT NULL | `RUNNING`/`COMPLETED`/`COMPLETED_WITH_ERRORS`/`FAILED` |
| `error_detail` | `text` NULL | |

`as_of_date`, `reference_watermark`, and `session_settings` together record the world a run saw. A
fresh evaluation derives new values and currently applicable rule versions. A replay instead
requires an original run with status `COMPLETED`, copies its scope and complete execution context,
and executes exactly the `rule_version_id` set recorded for that run.

`replay_of_rule_run_id` is null for fresh evaluations and points directly to the original completed
run for replays. Runs are append-only and deletion is restricted. `RUNNING`, `FAILED`, and
`COMPLETED_WITH_ERRORS` runs are not valid replay sources.

Finding uniqueness remains `UNIQUE (rule_version_id, scope_key, subject_key)`, so replay does not
duplicate persisted findings. Finding-set equality compares rule version, scope key, subject key,
offending value, observed value, expected value, and severity. The replay attempt and each rule's
outcome remain attributable through the new `rule_run` and `rule_run_rule_version` rows.

`COMPLETED_WITH_ERRORS` is a distinct status because revision 1 closed a run as `COMPLETED` when a
rule errored, making a partially-evaluated batch indistinguishable from a clean one in the
steward's headline view.

### `rule_run_rule_version`

| Column | Type | Notes |
|---|---|---|
| `rule_run_id` | `bigint` FK | |
| `rule_version_id` | `bigint` FK | |
| `outcome` | `text` NOT NULL | `EVALUATED` / `ERRORED` (FR-014) |
| `finding_count` | `integer` NOT NULL DEFAULT 0 | |
| `error_detail` | `text` NULL | |

PK `(rule_run_id, rule_version_id)`.

### `finding`

| Column | Type | Notes |
|---|---|---|
| `finding_id` | `bigint` PK | |
| `rule_run_id` | `bigint` FK NOT NULL | |
| `rule_version_id` | `bigint` FK NOT NULL | |
| `batch_id` | `bigint` FK **NULL** | null for aggregate subjects |
| `scope_key` | `text` NOT NULL | `b:<batch_id>` or `sp:<source_code>:<period>` — never null |
| `subject_type` | `text` NOT NULL | `record` / `batch` / `source_period` |
| `subject_key` | `text` NOT NULL | |
| `offending_value` | `text` NULL | |
| `observed_value` | `text` NULL | |
| `expected_value` | `text` NULL | |
| `severity` | `text` NOT NULL | snapshotted from the rule version |
| `detected_at` | `timestamptz` NOT NULL | |

Constraints:

```sql
UNIQUE (rule_version_id, scope_key, subject_key)
CHECK ((subject_type = 'record') = (batch_id IS NOT NULL))
CHECK (subject_type <> 'record' OR offending_value IS NOT NULL)   -- SC-002
```

**`scope_key` replaces `batch_id` in the uniqueness key.** Revision 1 used
`UNIQUE (rule_version_id, batch_id, subject_key)` with `batch_id NOT NULL`, which made a
never-arrived feed impossible to record — it has no batch — and would have re-fired the same missing
period against every subsequent batch, breaking FR-011 for the entire aggregate family. `scope_key`
is non-null for every subject type, so idempotency holds uniformly.

The second `CHECK` gives SC-002 a mechanism: every record-subject finding carries its offending
value or the insert fails.

`finding` rejects `UPDATE` and `DELETE` by trigger. It is the audit-bearing table; revision 1
protected `data_batch` and left this one mutable.

Indexes: `(scope_key, severity)`, `(rule_version_id)`, `(detected_at)`, `(subject_type, subject_key)`.

---

## Changes from revision 1

| # | Change | Defect it fixes |
|---|---|---|
| 1 | Master data becomes append-only version chains with `valid_from` + as-of resolution | Re-delivery duplicated every master record, so FR-016a/b flagged the whole file |
| 2 | `reference_watermark` + `as_of_date` bound into predicates and recorded on `rule_run` | Predicates joined unbounded master data, so SC-003 reproducibility was false in production while tests passed |
| 3 | `finding.batch_id` nullable, `scope_key` added, uniqueness rekeyed | A never-arrived feed had no batch to attach to; aggregate findings re-fired per batch |
| 4 | GiST exclusion constraint removed from `territory_alignment` | FR-019's overlap defect was uninsertable, so SC-001 could not cover it |
| 5 | Determinism enforced by `BEFORE INSERT` trigger using `provolatile = 'i'` | Denylist omitted `CURRENT_DATE` and ran only in application code that `dq_author` bypasses |
| 6 | Roles 5 → 7; bootstrap migration; conformance test | Seeding and rule registration had no role; the role set was unbounded |
| 7 | `COLLATE "C"` on composite-match columns | Collation change alters index order, so index and sequential scans disagree |
| 8 | Immutability trigger extended to batch member rows and `finding` | FR-003b was enforced on the container, not its contents |
| 9 | `correlation_id` on `rule_run` | Principle V requires one; retrofitting across five features is a backfill |
| 10 | `COMPLETED_WITH_ERRORS` status; `finding_count` per rule version | A partly-evaluated batch read as clean |
| 11 | `CHECK` tying `offending_value` to record subjects | SC-002 had no mechanism behind it |
| 12 | `ALTER DEFAULT PRIVILEGES` for every schema and verb | Pre-creating empty schemas achieved nothing without it |
