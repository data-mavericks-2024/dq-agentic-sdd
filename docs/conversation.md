# Conversation log

Running record of what we decided, what we built, and what tripped us up. Append a new session
section each working day. Newest session at the bottom.

---

## Session 1 — 2026-08-24

### Where we started

A single file, `SDD-PLAYBOOK.md`, containing a full Spec-Driven Development playbook for the
Agentic Data Quality Monitoring & Resolution Platform, written against GitHub Spec Kit. Plus
`notes.md`, a raw terminal transcript that duplicated it.

### Decisions made

**1. Keep Spec Kit.** Briefly explored dropping it and running SDD with hand-authored slash commands
and templates. Concluded the method survives without Spec Kit but you'd re-author the templates and
seven command files yourself — roughly two hours for full control. Decided against; Spec Kit stays.

**2. Stack locked.**

| Concern | Decision |
|---|---|
| Datastore | Supabase (managed PostgreSQL) — operational, quality, workflow, audit data all in one project |
| Agent orchestration | LangGraph with `PostgresSaver` checkpointer in the same database |
| Detection | SQL-based rules executed in PostgreSQL |
| Reasoning | Claude via the Anthropic API |
| Backend APIs | FastAPI |
| UI | Streamlit |
| Migrations | Alembic (never the Supabase dashboard) |
| Tests | Dedicated Supabase test project, schema-per-run isolation |

**3. Model IDs pinned.** `claude-sonnet-5` for investigation/impact/remediation reasoning,
`claude-haiku-4-5` for cheap classification and summarization. Swappable to `claude-opus-5` by
config change alone. Verified against the current model list, not from memory.

**4. No Docker.** Docker Desktop cannot be installed on this machine (corporate policy). This is the
single most consequential environment constraint:

- It rules out `testcontainers`, which the original plan specified for integration tests.
- Replacement: a dedicated Supabase **test project**; each test session creates a uniquely named
  schema, applies Alembic migrations into it, and drops it on teardown.
- Everything crosses the network, so expect that to dominate suite runtime.

**5. Supabase is fine as a cloud database — the local requirement was Docker, not Postgres.**
Cloud works for the app database. Trade-offs accepted: 30–80ms per query round trip on an
investigation agent that makes many sequential calls, free-tier throttling risk, and slower
reset/reseed cycles. One upside noted for later: Neon-style database branching would be a natural
fit for constitution principle 4 (simulate-before-propose) if Supabase branching proves usable —
worth revisiting at Feature 5.

### Playbook changes made

- Added a **Locked stack decisions** table so `/speckit-plan` has one source of truth.
- Added **constitution principle 11 — "The UI is not a trust boundary."** Streamlit makes it a
  two-line change to open a DB connection and write a row, which would silently bypass principles
  2, 4, and 6 at once. Principle 6 extended: the service-role key never reaches agent, API, or UI code.
- Rewrote the `/speckit-plan` prompt: Supabase hard constraints, four database roles
  (`dq_readonly`, `dq_sandbox`, `dq_publish`, `dq_migrate`), the no-Docker test strategy, and an
  API/UI architecture section.
- Feature 6 now includes a minimal Streamlit approval page — an approval gate with no way to approve
  isn't shippable. Full dashboard stays in Feature 7.
- Added a `boundary-reviewer` subagent and two more PreToolUse hook rules (service-role key outside
  migration tooling; SQL or connection strings inside the Streamlit package).
- Rewrote Open Items around the four decisions that will come up first.

### Environment problems hit, and the fixes

Worth reading before assuming a command is broken.

| Problem | Cause | Fix |
|---|---|---|
| `uvx` not recognized | uv was never installed | Installed uv 0.12.5 to `C:\Users\sushil.joshi\.local\bin` |
| `UnauthorizedAccess` on the install script | Execution policy was `Restricted` (both `CurrentUser` and `LocalMachine` were `Undefined`) | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` — bypass applies to the child process only |
| `uv` still not found after install | **VS Code captures env vars at application start.** A new terminal tab inherits the stale PATH | Fully quit and reopen VS Code, or `$env:Path = "C:\Users\sushil.joshi\.local\bin;$env:Path"` |
| `No such option: --ai` | Spec Kit renamed the flag | `--ai claude` → `--integration claude` |
| `specify init --help` not recognized | `specify` isn't installed standalone | Always prefix with `uvx --from git+https://github.com/github/spec-kit.git` |

### Two discoveries that contradicted the playbook

**1. Commands are namespaced `/speckit-*`** and install as *skills* in `.claude/skills/`, not as
commands in `.claude/commands/`. Every command name in the playbook was corrected. Two commands
exist that weren't in the playbook: `/speckit-checklist` (requirements-quality checklist, after
plan) and `/speckit-converge` (assesses the codebase, appends remaining work as tasks — useful when
implementation drifts from `tasks.md`). There's also `/speckit-taskstoissues` on disk.

**2. Spec Kit no longer creates git branches.** `create-new-feature.ps1` creates `specs/NNN-slug/`
and records the active feature in `.specify/feature.json` + `$env:SPECIFY_FEATURE`. Branch creation
is now **manual** — `git checkout -b 001-data-foundation`. Easy to forget; features will pile up on
`main` if you do.

Also noted: Spec Kit marks `/speckit-clarify` and `/speckit-analyze` as *optional*. For this project
they are not. In a regulated domain, spec ambiguity becomes plausible-looking wrong agent behavior.

### Repo state at end of session

```
.claude/settings.json          permission allowlist + deny rules
.claude/settings.local.json    session-local approvals (gitignored)
.claude/skills/speckit-*/      the 10 Spec Kit skills
.specify/                      templates, PowerShell scripts, memory, workflows
docs/sdd-playbook.md           the playbook (moved from repo root)
docs/mcp-postgres-proposal.md  proposed MCP server — NOT installed
docs/conversation.md           this file
CLAUDE.md                      stack, Supabase rules, testing, conventions
.env.example                   variable shape, no real values
.gitignore
```

Git initialized with `git init -b main`. `.specify/memory/constitution.md` is still the **unfilled
template** — Phase 1 replaces it.

### Deliberate non-actions

- **MCP Postgres server: proposed, not installed.** Node.js isn't present, and the allowlisted
  `psql` path needs no new credential surface. Revisit when Feature 1 produces schema worth
  exploring interactively. See `docs/mcp-postgres-proposal.md`.
- **`--preset healthcare-compliance` not applied.** Spec Kit ships it and it sounds relevant to
  pharma, but its contents are unknown and could conflict with the eleven hand-written principles.
  Inspect and cherry-pick deliberately rather than apply blind.

### Known weakness

The `psql` permission allowlist matches on the literal command string — it cannot verify which role
a connection actually uses. If `DQ_READONLY_URL` is populated with a privileged credential, the rule
allows writes anyway. **The database role is the real control; the allowlist is convenience.**

### Open questions

- [ ] Are two Supabase projects acceptable (dev + test)? One project means tests run against dev and
      a failed teardown leaves orphan schemas in the working database.
- [ ] Does the Supabase plan permit `CREATE ROLE`? Constitution principle 6 depends on it entirely.
- [ ] Row Level Security on the curated schema — on or off, and what compensates if off?
- [ ] How do stewards authenticate to Streamlit, and how does that identity reach the audit trail?
- [ ] Where do async agent runs execute — FastAPI background task or a separate worker — and how is
      a crashed run recovered from its checkpoint?

### Pick up here

1. Commit Phase 0:
   `git add -A ; git commit -m "Phase 0: Spec Kit scaffold, CLAUDE.md, permissions, env template"`
2. Run **Phase 1**: `/speckit-constitution` with the eleven principles from
   `docs/sdd-playbook.md`. Verify the generated `.specify/memory/constitution.md` before moving on —
   a vague principle here is expensive to fix at Feature 5.
3. Run **Phase 2**: the roadmap prompt (plain prompt, not a slash command) → `docs/roadmap.md`.
   Cross-check against the seven-feature table in the playbook.
4. Then **Feature 1** (`001-data-foundation`): create the branch manually, then
   `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` → `/speckit-tasks` →
   `/speckit-analyze` → `/speckit-implement`.

Feature 1 has zero agents by design — Postgres schema, rule registry, SQL rule runner, seeded defect
data. No LangGraph, no LLM calls, no UI. It's the trustworthy baseline everything else is measured
against.

---

## Session 2 — 2026-08-24

### Supabase project created

| | |
|---|---|
| Name | `FDB` |
| Ref | `ogsxigcineqwhjxocqmp` |
| Region | `ap-southeast-1` (Singapore) |
| Plan | Free, `t4g.nano` |
| State | Healthy, no migrations, no backups |

**One project, not two.** Recommendation had been two (dev + test) for blast-radius isolation, but
one is a defensible choice given the compensating control below. Decision recorded; not revisited.

### Consequence: schema isolation becomes load-bearing

With a single project, integration tests share the database with development. Isolation is by
schema and must be airtight:

- Each test session creates `test_<uuid>`, migrates into it, drops it on teardown.
- **A session-start sweep drops stale `test_*` schemas** left by crashed runs. This is not cleanup
  hygiene — it is the compensating control for sharing one project. Design it explicitly.
- No test writes outside its own schema. A test touching the curated schema is a defect.

### Supabase has THREE connection strings, not two

This corrects Session 1, which assumed a simple direct/pooled split.

| Mode | Host / port | Use |
|---|---|---|
| Direct | `db.<ref>.supabase.co:5432` | Preferred for stateful work — **IPv6-only on free tier** |
| Session pooler | `...pooler.supabase.com:5432` | IPv4-safe equivalent; one connection per client, so prepared statements and advisory locks work |
| Transaction pooler | `...pooler.supabase.com:6543` | Short read-only queries **only** |

Supabase made direct connections IPv6-only on free tier (IPv4 is a paid add-on). On an IPv4-only
network the direct string simply times out — **use the session pooler as the direct equivalent.**
Never the transaction pooler for Alembic or the checkpointer; it breaks them silently rather than
with an error.

Env vars restructured accordingly: `SUPABASE_DB_STATEFUL_URL` (direct *or* session pooler, whichever
is reachable) and `SUPABASE_DB_POOLED_URL` (transaction pooler). The old
`SUPABASE_DEV_*` / `SUPABASE_TEST_*` split is gone.

### Files updated

- `.env.example` — restructured for one project, three connection modes, `TEST_SCHEMA_PREFIX`
- `CLAUDE.md` — connection-mode table, rewritten testing section, new env var table
- `docs/sdd-playbook.md` — plan prompt now says one project and three connection strings; testing
  strategy includes the sweep; Open Items updated

### Also worth knowing

Free-tier projects pause after roughly a week of inactivity. A connection failure after a quiet week
is usually a paused project, not a broken config — check the dashboard before debugging.

The dashboard shows a branch selector (`main` / PRODUCTION) and "No branches". Supabase database
branching would be a natural fit for constitution principle 4 (simulate-before-propose) — worth
checking whether it's available on free tier when Feature 5 comes around.

### Database probe results — both open questions answered

Added `scripts/check_db.py` (run: `uv run --with "psycopg[binary]" python scripts/check_db.py`).
It reads `.env`, never prints the password, and verifies connection mode, prepared statements,
advisory locks, and `CREATE ROLE` in one pass.

```
SUPABASE_DB_STATEFUL_URL: session pooler (IPv4-safe)  aws-0-ap-southeast-1.pooler.supabase.com:5432
connected: PostgreSQL 17.6
prepared statements: OK
advisory locks:      OK
CREATE ROLE:         OK -- principle 6 is implementable
```

**Confirmed:**
- The network is **IPv4-only** — the direct connection is unreachable. The **session pooler**
  (port 5432 on the pooler host) is the stateful connection for Alembic and `PostgresSaver`.
- Prepared statements and advisory locks both work on the session pooler, as expected.
- `CREATE ROLE` is permitted → **constitution principle 6 is implementable** as written. This was
  the one that could have forced a redesign.
- Server is **PostgreSQL 17.6**, not 16.

### Connection-string gotchas hit along the way

| Symptom | Cause | Fix |
|---|---|---|
| `uv` not recognized (again) | VS Code's cached PATH — same as Session 1 | `$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"` |
| `missing "=" after "aws-0-..."`, host printed as `?` | The URL wasn't a valid URI — `[YOUR-PASSWORD]` placeholder left in, or an unencoded special character in the password | Keep the placeholder in the URL and set `SUPABASE_DB_PASSWORD` separately; the script percent-encodes and substitutes it |

Note: the session pooler requires the username to carry the project ref
(`postgres.ogsxigcineqwhjxocqmp`), unlike the direct connection. Easy to lose when hand-editing.

### Phase 1 complete — constitution ratified

`.specify/memory/constitution.md` is now **v1.0.0**, ratified 2026-08-24. All eleven principles
written in full, with a rationale paragraph each explaining what breaks if the principle is dropped.

**Gotcha worth remembering:** `/speckit-constitution` returned *"Unknown command"* at first. Cause:
Claude Code loads skills at **session start**, and the skills were installed by `specify init`
partway through the session. Fix is to restart Claude Code — not to reinstall Spec Kit.

Structure beyond the eleven principles:

- **Platform & Data Constraints** — single system of record, Alembic owns schema, session-scoped
  connection discipline, schema separation, the no-Docker reality, model IDs in one config module.
- **Development Workflow & Quality Gates** — spec-before-plan, clarification is mandatory
  (overriding Spec Kit's "optional" label), constitution check at plan time, golden scenarios
  before agents, per-feature quality bars.
- **Governance** — supremacy, amendment procedure, semantic versioning policy, two-point compliance
  review. Notably: amendments weakening principles II, IV, VI, or XI must state the compensating
  control that replaces the removed guarantee.

`CLAUDE.md` is explicitly declared **subordinate** to the constitution — where they conflict, the
constitution wins and CLAUDE.md gets corrected.

### Phase 2 complete — roadmap written

`docs/roadmap.md` — seven features, dependency-ordered, each with scope, explicit out-of-scope,
user-visible outcome, dependencies, and **the constitution principles it first puts into force**.
That last addition gives the plan-time constitution check a starting point instead of a blank page.

Matches the playbook's target slices. Decisions recorded in it:

- **Feature 6's UI line is drawn explicitly** — a pending-proposals list, the simulation result, and
  approve/reject with a required justification field. Nothing else. It exists because an approval
  gate with no way to approve isn't shippable. Everything richer is Feature 7.
- **Nothing before Feature 6 can write to curated data**, enforced by role grants created in
  Feature 1 rather than by convention.
- **Feature 2's clustering definition ("what is the same issue?") is flagged as the highest-risk
  unresolved decision** — two competent engineers build different systems from the same spec. Must
  be settled in `/speckit-clarify`, not during implementation.
- **Feature 4 depends on lineage that may not exist yet.** If the real downstream reports and KPIs
  aren't identified by then, the feature shrinks to the lineage that genuinely exists rather than
  inventing a model of it.

### Still open

- [ ] RLS on the curated schema — on or off, and what compensates if off
- [ ] Steward authentication for Streamlit, and how that identity reaches the audit trail
- [ ] Where async agent runs execute, and crash recovery from checkpoint
- [ ] Identify real downstream reports/KPIs for Feature 4 lineage
- [ ] Confirm source systems and feed cadence for the timeliness rules

### Next: Feature 1

```powershell
git checkout -b 001-data-foundation     # Spec Kit does NOT do this for you
```

Then `/speckit-specify` with the Feature 1 prompt from `docs/sdd-playbook.md`. Feature 1 has zero
agents by design — schema, rule registry, SQL rule runner, seeded defect data, and the four database
roles. No LangGraph, no model calls, no UI.
