# Nightly Commercial Load Demo

This is a narrow management-demo slice over the verified Feature 001 foundation. It uses live
synthetic data in Supabase and the real deterministic rule runner. It does not implement an AI
agent, remediation, approval, or publish workflow.

## End-to-end path

1. An operations command registers the shipped rule library through `dq_author`.
2. When the database has no batches, it seeds the deterministic three-period synthetic world
   through `dq_ingest` and the feed catalogue through `dq_author`.
3. The nightly action runs batch-scoped rules for every delivered batch in the selected period.
4. It also runs source-period rules for every configured source, including a source whose expected
   feed never arrived.
5. The browser reloads persisted batch, run, outcome, finding, rule-version, and ownership evidence
   through the backend's `dq_readonly` connection.

The UI has no database credential. Its only state-changing route accepts a canonical month and
delegates to the existing `dq_engine` runner. It accepts no SQL, rule text, table name, or arbitrary
filter expression.

## Supabase tables used

No demo-only table or dashboard-created object is required. Alembic revisions `0001` through
`0013` create the complete backend.

| Schema | Tables used in this demo |
|---|---|
| `commercial` | `source_system`, `data_batch`, `hcp`, `hco`, `product`, `territory`, `territory_alignment`, `sales_transaction` |
| `dq` | `rule`, `rule_version`, `feed_expectation`, `rule_run`, `rule_run_rule_version`, `finding` |

`data_batch`, the commercial member tables, `rule_version`, and `finding` carry the immutable facts.
`rule_run` and `rule_run_rule_version` carry execution status, pinned context, lineage, and outcomes.

## Codespaces setup

Use the existing Codespaces secrets. Do not print their values. The demo needs these names:

- `SUPABASE_DB_STATEFUL_URL`
- `DQ_MIGRATE_URL`
- `DQ_INGEST_URL`
- `DQ_AUTHOR_URL`
- `DQ_ENGINE_URL`
- `DQ_READONLY_URL`

Pull the feature branch and prepare the database:

```bash
cd /workspaces/dq-agentic-sdd
git pull --ff-only origin 001-data-foundation
git rev-parse --short HEAD

DQ_SCHEMA_PREFIX= UV_LINK_MODE=copy uv run alembic upgrade head
DQ_SCHEMA_PREFIX= UV_LINK_MODE=copy uv run dq demo prepare
```

The preparation command does not reseed a database that already contains batches. It registers the
current shipped rules and executes the latest business period so the UI opens with evidence.

Start the demo server:

```bash
DQ_SCHEMA_PREFIX= UV_LINK_MODE=copy uv run dq demo serve --host 0.0.0.0 --port 8000
```

In the Codespaces **Ports** tab, keep port 8000 private and open it in the browser.

## Management demonstration

1. **Command center**: identify the selected month, immutable source batches, records evaluated,
   persisted findings, exact rules evaluated, duration, and reference watermark.
2. **Run deterministic rules**: explain that the backend evaluates delivered batches and checks
   expected feeds for every source. On a repeat run, new findings can be zero while the persisted
   finding count stays constant; the database uniqueness constraint proves idempotency.
3. **Findings**: filter by domain and severity, then open one row. Show the failing subject, rule
   version, owner, observed evidence, and expected value.
4. **Rule library**: show that failure logic is versioned SQL configuration, not an AI decision.
5. **Run evidence**: show the scoped run IDs, watermarks, rule outcomes, and correlation identities.
6. **Governance**: close with the structural controls and the capabilities intentionally deferred
   to later features.

## Honest boundaries

- The data is synthetic and must be described as synthetic during the demo.
- The interface is a management-demo surface, not the final Feature 7 dashboard.
- No agent runs in this slice.
- No proposal, approval, correction, or publish action exists in this slice.
- Feature 001 remains incomplete until its remaining tasks and constitution gate pass.
