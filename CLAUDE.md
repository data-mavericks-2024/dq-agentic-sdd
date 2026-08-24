# CLAUDE.md

Agentic Data Quality Monitoring & Resolution Platform — pharmaceutical commercial data.

Deterministic SQL rules detect data-quality failures. Tool-using agents investigate root causes,
assess business impact, and propose corrections. A human steward approves before anything is
published. Agents never decide whether a record failed, and never write to curated data.

**No application code exists yet.** The repo is at Phase 0 of the SDD workflow.

---

## Read this before planning or implementing

`.specify/memory/constitution.md` holds eleven non-negotiable principles. Read it at the start of
any `/speckit-plan`, before any schema or graph design, and during every post-implementation check.
Cite the specific design element that enforces each principle, or flag it as unenforced. A plan that
satisfies a principle only by prompt instruction, rather than structurally, does not satisfy it.

`docs/sdd-playbook.md` is the operating manual for this project: the phase-by-phase prompts, the
seven-feature roadmap, and the reasoning behind the stack choices.

---

## Spec-driven workflow

Commands are Spec Kit skills, namespaced `/speckit-*`:

| Step | Command | Output |
|---|---|---|
| 1 | `/speckit-constitution` | `.specify/memory/constitution.md` |
| 2 | `/speckit-specify` | `specs/NNN-slug/spec.md` |
| 3 | `/speckit-clarify` | updated `spec.md` — **never skip this one** |
| 4 | `/speckit-plan` | `plan.md`, `data-model.md`, `research.md`, `contracts/`, `quickstart.md` |
| 5 | `/speckit-tasks` | `tasks.md` |
| 6 | `/speckit-analyze` | cross-artifact consistency report |
| 7 | `/speckit-implement` | code + tests |

**How the active feature is tracked:** `create-new-feature.ps1` writes `.specify/feature.json` and
sets `$env:SPECIFY_FEATURE` / `$env:SPECIFY_FEATURE_DIRECTORY`. It does **not** create a git branch.
Create the branch manually to match the feature slug (`git checkout -b 001-data-foundation`) so one
feature stays one branch, one spec, one reviewable unit.

`/speckit-specify` describes WHAT and WHY only. The moment LangGraph, Supabase, FastAPI, or
Streamlit appears in a spec, that spec has become a plan. Say "the database" and "the user
interface" instead.

---

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Dependencies | uv |
| Agent orchestration | LangGraph, `PostgresSaver` checkpointer |
| Datastore | Supabase (managed PostgreSQL) — commercial data, DQ metadata, workflow state, audit |
| Schema | SQLAlchemy Core + Alembic |
| Detection | SQL rules executed in PostgreSQL |
| Reasoning | Claude via the Anthropic API (`anthropic` Python SDK) |
| Backend API | FastAPI |
| UI | Streamlit |
| Tests | pytest |
| Lint / types | ruff, mypy strict |

**Model IDs** live in one config module and are never inlined:
`claude-sonnet-5` for investigation, impact, and remediation reasoning; `claude-haiku-4-5` for
classification and summarization. The reasoning tier must be swappable to `claude-opus-5` by config
change alone. Use adaptive thinking (`thinking={"type": "adaptive"}`) on reasoning nodes, and stream
any call with a large `max_tokens` so long investigations do not hit HTTP timeouts.

---

## Supabase working rules

Violating any of these produces silent runtime failures that look like random flakiness rather than
configuration errors.

**1. Never use the transaction pooler for anything stateful.**
Supabase exposes three connection strings. Alembic, the LangGraph `PostgresSaver`, and any code
relying on prepared statements or advisory locks MUST use `SUPABASE_DB_STATEFUL_URL`:

| Mode | Port / host | Use |
|---|---|---|
| Direct | `5432` on `db.<ref>.supabase.co` | Preferred for stateful work — **IPv6-only on free tier** |
| Session pooler | `5432` on `...pooler.supabase.com` | IPv4-safe equivalent of direct; one connection per client, so prepared statements and advisory locks work |
| Transaction pooler | `6543` on `...pooler.supabase.com` | Short read-only queries **only** — breaks prepared statements and advisory locks |

**Confirmed for this machine (2026-08-24):** the network is IPv4-only, so the **session pooler** is
the working stateful connection — `aws-0-ap-southeast-1.pooler.supabase.com:5432`. Prepared
statements and advisory locks both verified working on it. Server is PostgreSQL 17.6.

The failure mode of getting this wrong is not an error message — it is intermittent, misleading
breakage. Verify with `uv run --with "psycopg[binary]" python scripts/check_db.py`.

**2. The service-role key never reaches application code.**
It is not referenced from agent code, from FastAPI request handlers, or from Streamlit. It exists
only in the migration tooling's environment. Agents connect under dedicated least-privilege database
roles: `dq_readonly` (investigation), `dq_sandbox` (sandbox schema writes), `dq_publish` (curated
writes, post-approval only), `dq_migrate` (Alembic). No role inherits another's privileges.

**3. Alembic owns the schema.**
No schema change is ever made through the Supabase dashboard. Every DDL change is a reviewed
migration in version control, reproducible across the dev and test projects.

**4. There is no local Postgres and no Docker on this machine.**
Do not propose `testcontainers`, `docker-compose`, or `supabase start`. Any design that requires
them is not implementable here.

---

## Testing

`testcontainers` is unavailable (no Docker), and there is **one** Supabase project — so integration
tests run against the same database as dev. Isolation is by schema, and it has to be airtight.

- Each test session creates `test_<uuid>`, applies Alembic migrations into it, and drops it on
  teardown. Concurrent runs cannot collide.
- **A session-start sweep drops stale `test_*` schemas** older than a few hours, left behind by
  crashed runs. Without it, a hard-killed test run silently accumulates schemas in the working
  database. This is not optional — it is the compensating control for having one project.
- No test ever writes outside its own schema. A test that touches the curated schema is a defect,
  not a slow test.
- Unit tests for rules, tools, and graph nodes run against a stubbed model with no network access.
- Golden-scenario tests are the only tests permitted to call a real model. Mark them separately
  (`-m golden`) so the default test command stays fast and free.
- Every test crosses the network to Singapore. Expect this to dominate suite runtime.

Free-tier projects pause after roughly a week of inactivity. A sudden connection failure after a
quiet week is usually a paused project, not a broken config — check the dashboard first.

---

## Commands

```powershell
uv sync                          # install dependencies
uv run pytest                    # default suite (stubbed model, no API cost)
uv run pytest -m golden          # golden scenarios (calls a real model — costs tokens)
uv run ruff check . ; uv run ruff format .
uv run mypy .                    # strict
uv run alembic upgrade head      # migrations, direct connection only
uv run uvicorn app.api.main:app --reload
uv run streamlit run app/ui/Home.py
```

---

## Environment variables

Never commit real values. `.env` is gitignored; `.env.example` documents the shape.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `SUPABASE_PROJECT_REF` | `ogsxigcineqwhjxocqmp` — not a secret |
| `SUPABASE_DB_STATEFUL_URL` | Alembic, checkpointer, anything stateful (direct or session pooler) |
| `SUPABASE_DB_POOLED_URL` | transaction pooler, short read-only queries only |
| `TEST_DATABASE_URL` | integration tests — same database, stateful connection |
| `TEST_SCHEMA_PREFIX` | `test_` |
| `DQ_READONLY_URL` | investigation agents (`dq_readonly` role) |
| `DQ_SANDBOX_URL` | sandbox simulation (`dq_sandbox` role) |
| `DQ_PUBLISH_URL` | governed publish (`dq_publish` role), post-approval only |
| `DQ_MIGRATE_URL` | Alembic (`dq_migrate` role) |

The four `DQ_*_URL` roles don't exist yet — they're created by the Feature 1 migration that defines
them. Leave them blank until then.

The service-role key deliberately has no entry here. If you find yourself needing one, that is a
design error, not a missing variable.

---

## Conventions

- Every agent tool is a plain Python function with a Pydantic-validated signature running
  parameterized SQL. No free-form SQL string from a model ever reaches the database. If dynamic
  querying is genuinely needed, expose a constrained query builder with an allowlist of tables,
  columns, and predicates, validated before execution.
- Structured outputs are Pydantic-validated at every agent boundary.
- Control flow lives in explicit LangGraph nodes and conditional edges, not in model free-choice.
  Agentic nodes are bounded ReAct loops with explicit `recursion_limit` and a cost guard.
- FastAPI owns every state-changing operation and re-validates steward authority, proposal
  freshness, and constitution compliance server-side on every call. Streamlit holds no database
  credential, issues no SQL, and imports no agent code.
- Every agent step is persisted to an append-only audit table with a correlation ID.
