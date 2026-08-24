# Contract — Rule Definition

The interface a data engineer authors against. This is the feature's primary external contract:
SC-007 requires that registering a rule produce findings on the next run **without any change to
engine code**.

**Revision 2** — predicate references are now unqualified, determinism is enforced by a database
trigger, and master-data joins must bind the as-of parameters.

## Definition shape

```yaml
rule_key: HCP-NPI-FORMAT          # unique, stable, referenced by findings forever
domain: HCP                       # SALES | HCP | HCO | PRODUCT | TERRITORY_ALIGNMENT
dimension: validity               # completeness | uniqueness | validity | referential_integrity
                                  #   | timeliness | consistency | conformity
severity: HIGH                    # HIGH | MEDIUM | LOW
owning_function: Master Data Management
subject_type: record              # record | batch | source_period
parameters: {}
predicate_sql: |
  SELECT h.hcp_id::text  AS subject_key,
         h.npi           AS offending_value,
         NULL::text      AS observed_value,
         NULL::text      AS expected_value
  FROM   hcp h
  WHERE  h.batch_id = :batch_id
    AND (h.npi IS NULL OR h.npi !~ '^[0-9]{10}$')
```

**Table references are unqualified.** `hcp`, not `commercial.hcp`. Schema resolution happens through
a `search_path` the engine pins per run (research.md D10), which is what allows the same predicate
to run against `commercial` in development and `test_20260824143022_a1b2c3_commercial` under test
without any string rewriting. Revision 1 hard-coded schema names, which made prefixed test isolation
and the no-interpolation guarantee mutually exclusive.

## Predicate output contract

Exactly these four columns, any order, these names:

| Column | Type | Meaning |
|---|---|---|
| `subject_key` | `text` | Identifies the failing subject: a record id for `record`; a batch id for `batch`; `<source_code>:<period>` for `source_period`. |
| `offending_value` | `text` | The value that failed. **Required** for `subject_type: record` — a database `CHECK` rejects a null one. |
| `observed_value` | `text` NULL | What was seen. For aggregate subjects. |
| `expected_value` | `text` NULL | What was expected. For aggregate subjects. |

One returned row becomes one finding. **A predicate returns failures, never passes** — returning
every row with a verdict column would move a million rows across the link per rule.

## Engine-bound parameters

Bound automatically; never declare them in `parameters`:

| Parameter | Meaning |
|---|---|
| `:batch_id` | The subject batch. Null for `source_period` scope. |
| `:as_of_date` | Which master-data version applies. |
| `:reference_watermark` | The highest `batch_id` visible to this run. |

Anything in `parameters` is bound by name. String interpolation into `predicate_sql` never happens.

## Joining master data — the required idiom

Master tables are append-only version chains (research.md D9). **Every join to `hcp`, `hco`,
`product`, or `territory` must resolve as-of and must bind both parameters:**

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

**Omitting `:reference_watermark` is the most dangerous mistake available in this contract.** The
predicate still returns plausible results, still passes the determinism test — which runs twice
within the same minute — and silently breaks reproducibility for every historical re-run. This was
the defect that made revision 1's central reproducibility claim false. The registration trigger
therefore rejects any predicate referencing a master table without binding both parameters.

`ORDER BY … LIMIT 1` terminates in `valid_from`, unique per natural key, so the tie-break is total.

## Validation — enforced by trigger, not by convention

A `BEFORE INSERT` trigger on `dq.rule_version` raises unless all hold. It is in the database
because `dq_author` can `INSERT` here directly; a check living only in the Python registration path
would be a convention, which constitution principle I does not accept.

1. **Single statement.** One `SELECT`. No semicolons, no multiple statements, no CTE-with-DML.
2. **Exact projection.** The four contract columns, correctly named.
3. **Every expression function is IMMUTABLE** — `pg_proc.provolatile = 'i'`. This replaces
   revision 1's denylist of six names, which omitted `CURRENT_DATE`, `LOCALTIMESTAMP`, `LOCALTIME`,
   `CURRENT_TIME`, `statement_timestamp()`, `transaction_timestamp()`, and one-argument `age()`.
   Note that rejecting only VOLATILE functions would also miss them — `now()` and `current_date` are
   STABLE. An allowlist over IMMUTABLE is complete by construction; a denylist is incomplete by
   nature.
4. **No function calls in `FROM`.** Keeps rule 3 total, with no carve-out for table-reading helpers.
5. **Unqualified table references, from an allowlist.** No `pg_catalog`, no `information_schema`, no
   schema qualification.
6. **Total ordering where order decides the result.** Every `LIMIT`, `DISTINCT ON`, and ranking
   window function needs an `ORDER BY` terminating in a unique key. Revision 1 checked only `LIMIT`,
   missing `DISTINCT ON` and `row_number()` — which is exactly how a rule author picks the surviving
   record in the duplicate families, where ties are the whole subject matter.
7. **Master-data joins bind both as-of parameters.**
8. **Parameters declared.** Every `:name` appears in `parameters` or is engine-bound.

## Versioning semantics

Changing `predicate_sql`, `parameters`, `severity`, or `subject_type` creates a **new
`rule_version`**; the previous version stays readable forever so past findings remain interpretable
(FR-006, SC-005).

Changing `owning_function` or `is_active` does **not** create a version — neither changes what a
finding means. Deactivating stops new findings without touching historical ones (FR-007, SC-006).

For FR-016b, the normalisation itself is part of the predicate, so changing it necessarily creates
a version — satisfying FR-016c without a separate mechanism.

## Registration outcomes

| Condition | Result |
|---|---|
| Valid, `rule_key` unseen | Rule created, version 1 |
| Valid, `rule_key` exists, semantic fields changed | New version appended |
| Valid, `rule_key` exists, nothing semantic changed | No-op |
| Any validation rule fails | Trigger raises; `RuleDefinitionError` names the failing rule and the offending fragment |
| Missing `domain`, `dimension`, `severity`, or `owning_function` | Rejected by `NOT NULL` (FR-005) |
