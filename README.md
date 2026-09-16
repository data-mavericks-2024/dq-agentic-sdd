# Deterministic Data-Quality Foundation

Feature 1 of the Agentic Data Quality Monitoring & Resolution Platform: a versioned rule registry
and a deterministic rule engine over pharmaceutical commercial data (sales transactions, HCP/HCO
master, product master, territory alignment). No AI agent, no correction, no UI — see
`.specify/memory/constitution.md` and `docs/roadmap.md` for what comes after this feature and why.

Full walkthrough, scenario by scenario: [`specs/001-data-foundation/quickstart.md`](specs/001-data-foundation/quickstart.md).

## Setup

```bash
uv sync
cp .env.example .env   # fill in the values below
uv run --with "psycopg[binary]" python scripts/check_db.py
uv run alembic upgrade head
```

`check_db.py` must report `prepared statements: OK`, `advisory locks: OK`, `CREATE ROLE: OK` before
you run anything else. A free-tier Supabase project pauses after roughly a week of inactivity — a
sudden connection failure after a quiet week is almost always that, not a broken `.env`.

`alembic upgrade head` creates the five schemas, every table, the determinism trigger, and — only
when no schema prefix is set — the seven database roles via the bootstrap migration. Capture the
seven `DQ_*_URL` connection strings into `.env` immediately afterward; those roles, and their
connection strings, do not exist until this command has run once.

## The three Supabase connection modes

Supabase exposes three ways to reach the same database. Only one of them is safe for this project's
stateful work.

| Mode | Port / host | Use here |
|---|---|---|
| Direct | `5432` on `db.<ref>.supabase.co` | Preferred in general, but **IPv6-only on the free tier** — unusable on an IPv4-only network |
| **Session pooler** | `5432` on `...pooler.supabase.com` | **What this project uses.** One connection per client, so prepared statements and advisory locks work. `SUPABASE_DB_STATEFUL_URL` points here |
| Transaction pooler | `6543` on `...pooler.supabase.com` | Short read-only queries *only* — silently breaks prepared statements and advisory locks. `src/dq/db/engine.py` raises at startup if a `DQ_*_URL` ever points at port 6543 |

Getting this wrong does not raise an error — it produces intermittent, misleading breakage days
later. If a network is IPv6-capable, the direct endpoint also works; this codebase was verified
against the session pooler specifically (`aws-0-ap-southeast-1.pooler.supabase.com:5432`,
PostgreSQL 17.6).

## The seven database roles

Constitution v1.1.0 principle VI declares this list exhaustive; `tests/integration/test_role_conformance.py`
fails the build if the database ever has an eighth `dq_*` role.

| Role | Env var | Purpose | `commercial` | `dq` |
|---|---|---|---|---|
| `dq_migrate` | `DQ_MIGRATE_URL` | Alembic only | OWNER (DDL) | OWNER |
| `dq_ingest` | *(none — used only by the seed generator)* | Loads source data | SELECT, INSERT | — |
| `dq_author` | *(none — CLI connects directly)* | Registers/versions/deactivates rules | — | SELECT; INSERT/UPDATE on `rule`, `rule_version` |
| `dq_engine` | *(none — the rule runner connects directly)* | Executes rules, writes findings | SELECT only | SELECT; INSERT on `finding`, `rule_run`, `rule_run_rule_version` |
| `dq_readonly` | `DQ_READONLY_URL` | Querying findings; Feature 3's investigation agents | SELECT | SELECT |
| `dq_sandbox` | `DQ_SANDBOX_URL` | Feature 5 simulation (unused this feature) | SELECT | SELECT |
| `dq_publish` | `DQ_PUBLISH_URL` | Feature 6 governed writes (unused this feature) | SELECT, INSERT | SELECT |

No role inherits another's privileges (`NOINHERIT`). The service-role key is never used by
application code — see `CLAUDE.md` if you find yourself reaching for it, that is a design error,
not a missing variable. Full grant matrix: `specs/001-data-foundation/data-model.md`.

## Everyday commands

```bash
uv run dq seed --periods 3 --with-defects   # synthetic data, three periods, injected defects
uv run dq rules register --all              # register the shipped rule library
uv run dq run-rules --batch-id 1            # evaluate active rules against one arrival
uv run dq findings --batch-id 1             # query findings, filtered any way you like
uv run dq summarise --batch-id 1            # per-scope counts by domain, rule, severity
uv run dq admin sweep-test-schemas --older-than 0   # manual test-schema cleanup

uv run pytest                    # default suite: fast, no network-heavy volume test
uv run pytest -m volume          # SC-008 performance check (~1M rows, several minutes)
uv run ruff check . ; uv run ruff format . ; uv run mypy .
```

## Rollback procedure (verified)

**The migrate-only boundary.** Every destructive `downgrade()` runs as `dq_migrate` — each one
calls `assume_migrate_role`/`reset_role` internally (`src/dq/db/migration_support.py`) — so rolling
back requires `DQ_MIGRATE_URL`, the same credential Alembic itself uses. No other role can run a
migration downgrade.

**The rollback boundary is one step, not "back to nothing."** Five migrations —
`0006`, `0007`, `0008`, `0010`, `0012` — declare an explicit no-op `downgrade()`. Each one installed
a safety tightening (a predicate-validator rule, removal of a mutable session registry) that must
never be silently weakened by rolling back past it; `alembic downgrade base` would skip them, not
undo them. The rollback this feature actually offers is **one migration at a time**, and only for a
migration that declares a real, working `downgrade()`.

**Verified**, in an isolated prefixed test schema (`tests/integration/test_migration_downgrade_restore.py`):
a fresh install through every migration to head, `alembic downgrade` one step (`0013` → `0012`,
removing the `rule_run.replay_of_rule_run_id` column and its restrictive foreign key), then
`alembic upgrade head` again — and the schema this produces is byte-for-byte the same shape it was
before the downgrade. That test is part of the default suite; a regression in either direction
fails it.

**Before downgrading a database with real data in it:**

1. **Check for a running rule run first.** `SELECT * FROM dq.rule_run WHERE status = 'RUNNING'` —
   a migration that alters a table an in-flight run is writing to is a race, not a rollback.
2. **`finding` and `rule_version` are immutable by trigger, and downgrading does not change that.**
   A downgrade that removes a column (like `0013`'s) discards data in that column permanently —
   there is no soft-delete to reverse. If the column matters, export it first:
   `COPY (SELECT rule_run_id, replay_of_rule_run_id FROM dq.rule_run WHERE replay_of_rule_run_id
   IS NOT NULL) TO STDOUT WITH CSV HEADER` (adjust per migration).
3. **Findings are audit records, not application state.** No migration in this feature drops the
   `finding` table itself or narrows what it can express — only `0013`'s addition to `rule_run` has
   a real downgrade — so a rollback never discards a finding, only the replay-lineage metadata
   describing how a later run related to an earlier one.
4. Run the downgrade, then `alembic upgrade head` to confirm you can get back, exactly as the test
   above does, before treating the database as settled at the lower revision.

## Test isolation

See `specs/001-data-foundation/quickstart.md` § Test isolation for the full explanation. In short:
every test session creates its own `test_<timestamp>_<uuid>_*` schemas, migrates into them, and
drops them on teardown; a session-start sweep removes stale schemas from crashed runs. No test
writes outside its own prefixed schemas, and no test session creates or drops the seven roles —
those are cluster-global and shared with development.
