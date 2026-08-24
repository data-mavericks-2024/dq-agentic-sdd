# Contract — Rule Runner and Findings Access

The operational surface of the feature. No HTTP, no UI — a library API plus a thin CLI.

**Revision 2** — runs take a scope rather than a batch, session settings are pinned and recorded,
errored rules are surfaced, and the summary no longer double-counts across rule versions.

## Library API

```python
def run_rules(scope: RunScope, *, rule_keys: Sequence[str] | None = None) -> RuleRunResult: ...
def summarise_scope(scope: RunScope) -> ScopeSummary: ...
def summarise_batch(batch_id: int) -> ScopeSummary: ...   # convenience wrapper over BatchScope
def query_findings(
    *, batch_id: int | None = None, domain: str | None = None,
    rule_key: str | None = None, severity: str | None = None,
    detected_from: datetime | None = None, detected_to: datetime | None = None,
) -> list[Finding]: ...
```

`RunScope` is one of:

```python
BatchScope(batch_id: int)
SourcePeriodScope(source_system_code: str, period: DateRange)
```

**A run takes a scope, not a batch.** Revision 1's `run_rules(batch_id)` could not express "check
whether the September feed from Veeva ever arrived" — the case that has no batch by definition, and
that FR-021d and SC-009 require.

All return values are Pydantic models. `query_findings` supports any combination of filters
(FR-023).

## `run_rules` semantics

1. Resolve `as_of_date` (the scope's business-period end) and `reference_watermark`
   (`max(batch_id)` visible now).
2. Open a `rule_run` row with status `RUNNING`, a fresh `correlation_id`, and the resolved
   `as_of_date`, `reference_watermark`, and `session_settings`.
3. Take an advisory lock on `(scope_key, rule_version_id)` per rule.
4. Pin the session (research.md D10):
   ```sql
   SET LOCAL TimeZone = 'UTC';
   SET LOCAL DateStyle = 'ISO, YMD';
   SET LOCAL search_path = <prefix>commercial, <prefix>dq;
   SET LOCAL statement_timeout = '15min';
   ```
5. For each active rule version whose `subject_type` matches the scope, execute one statement:

   ```sql
   INSERT INTO finding (rule_run_id, rule_version_id, batch_id, scope_key, subject_type,
                        subject_key, offending_value, observed_value, expected_value,
                        severity, detected_at)
   SELECT :run_id, :rule_version_id, :batch_id, :scope_key, :subject_type,
          p.subject_key, p.offending_value, p.observed_value, p.expected_value,
          :severity, now()
   FROM ( <predicate_sql> ) AS p
   ON CONFLICT (rule_version_id, scope_key, subject_key) DO NOTHING
   ```

6. Record `EVALUATED` or `ERRORED` plus `finding_count` in `rule_run_rule_version`.
7. Close the run: `COMPLETED`, `COMPLETED_WITH_ERRORS`, or `FAILED`.

**One statement per rule, not per record.** This is what meets SC-008 across the Singapore link.

**The advisory lock matters.** Without it, two concurrent runs over one scope race for which
`rule_run_id` is stamped on each finding, making "which run produced this?" non-deterministic — in
the audit trail, which is the one place that must never be ambiguous.

**`detected_at` uses `now()` in the INSERT, not in the predicate.** Predicates cannot call `now()`
at all (rule-definition.md validation rule 3); the engine stamps the time outside the predicate so
re-running never changes which rows are returned.

## Error handling

**A failing rule does not fail the run.** Its outcome is recorded as `ERRORED` with detail, and the
remaining rules still execute.

**But the run does not close as `COMPLETED`.** If any rule errored, status is
`COMPLETED_WITH_ERRORS`, and `summarise_batch` reports errored rules as a first-class line. Revision
1 closed such a run as `COMPLETED` with no summary line, making a partially-evaluated batch
indistinguishable from a clean one in the steward's headline view — the more dangerous of the two
failure modes, and the one the spec's own edge case warns against.

**Error granularity is per rule, not per record.** Set-based execution cannot do otherwise: one
malformed value aborts the statement, and the rule contributes zero findings for the entire scope —
including records that would legitimately have failed. `spec.md` § Edge Cases is amended to describe
this honestly rather than promising per-record error reporting the architecture cannot deliver.

## Idempotency

Re-running the same rule versions over the same scope inserts nothing new — guaranteed by
`UNIQUE (rule_version_id, scope_key, subject_key)` plus `ON CONFLICT DO NOTHING`, not by an
application-side check (FR-011, SC-003). A crashed run is recovered by re-running it.

**Idempotency is not the same as reproducibility.** The constraint guarantees non-duplication. That
the finding *set* is identical comes from pinning `as_of_date`, `reference_watermark`, and
`session_settings` — recorded on `rule_run` so two runs can be compared rather than assumed equal.
Conflating the two was revision 1's central error.

## Connection and role

`run_rules` connects as **`dq_engine`** over the stateful connection, and asserts
`SELECT current_user = 'dq_engine'` at connection open. That role has no write privilege on
`commercial`, so constitution principle II is enforced by the database — but only if the connection
is what it claims to be, which is why the assertion exists. Without it, pointing `DQ_ENGINE_URL` at
`dq_migrate` voids the guarantee silently.

## `summarise_scope`

Returns counts grouped by domain, by rule, and by severity, plus a **rules-errored** line (FR-024).

**Aggregate findings are included, not filtered out.** A batch summary includes the `source_period`
findings whose scope covers that batch's source system and business period. Revision 2 filtered on
`batch_id`, which is null for every aggregate finding — so a feed that never arrived appeared in no
summary at all. That is the failure least likely to be noticed by other means: nothing looks wrong,
the numbers simply go quiet (FR-024a, SC-009).

**Counts are per rule, not per rule version.** A scope evaluated under rule versions 1 and 2 holds
findings from both by design — but reporting them separately shows the same real problem twice, and
the double-count grows with every threshold change. The summary counts distinct `subject_key` per
`rule_id`. Revision 1 aggregated by rule version, and SC-004's reconciliation check could not catch
it, because that check compares the summary to the findings rather than to reality.

Computed by aggregation, never cached, so SC-004's reconciliation holds by construction.

## CLI

```bash
dq seed --periods 3 --with-defects
dq run-rules --batch-id 42
dq run-rules --source VEEVA --period 2026-09
dq run-rules --batch-id 42 --rule HCP-NPI-FORMAT
dq summarise --batch-id 42
dq findings --batch-id 42 --severity HIGH
dq rules register path/to/rule.yaml
dq rules deactivate HCP-NPI-FORMAT
dq admin sweep-test-schemas --older-than 4h
```

**Development and operations only. This CLI never reaches a steward machine.** It holds
`dq_migrate` and `dq_author` credentials, so once Feature 6 puts the approval gate behind FastAPI,
this CLI would be a path around every server-side re-validation principle XI requires. Stated here
in writing because the constraint is a decision, not an accident of packaging.

For Feature 1 it makes no trust decision and gates no approval, so principle XI does not apply to
it.

## Error contract

| Condition | Raised |
|---|---|
| Scope resolves to no batch and no feed expectation | `ScopeNotFoundError` |
| Predicate fails to execute | Recorded `ERRORED`; run continues; status becomes `COMPLETED_WITH_ERRORS` |
| Registration validation fails | Trigger raises; surfaced as `RuleDefinitionError` |
| Connected role is not `dq_engine` | `RoleAssertionError` at connection open |
| Attempted write to `commercial` as `dq_engine` | `InsufficientPrivilege` — surfaced, never caught and ignored |
