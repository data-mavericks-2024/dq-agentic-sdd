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

### Feature 1 — spec written and clarified

Branch `001-data-foundation`. `specs/001-data-foundation/spec.md` + `checklists/requirements.md`.
Quality checklist **16/16 passing**.

`/speckit-specify` produced four prioritised user stories (detection → rule governance →
reproducibility → querying), 27 functional requirements, 7 edge cases, 9 assumptions. It left three
`[NEEDS CLARIFICATION]` markers rather than guessing.

`/speckit-clarify` resolved those three and found two more. Five decisions, all recorded in the
spec's `## Clarifications` section:

| # | Decision |
|---|---|
| 1 | **A data batch is one physical arrival** — one file or load event, immutable once recorded. A correction arrives as a *new* batch. Reproducibility then comes for free. |
| 2 | **Duplicate HCP detection is two rules**, not one: NPI collision at High severity (near-certain), deterministic composite match on name + postal code + licence state at Medium (suspected). No fuzzy or probabilistic matching — that would breach principle I. |
| 3 | **A feed expectation catalogue is in scope** (source, cadence, delivery window, active date range). Inferring expectations from history cannot detect a feed that never arrived. |
| 4 | **Target scale: ~1M sales transactions/month, ~100K HCPs, ~5K products.** Forces set-based rule evaluation from day one — critical because the database is across a network in Singapore. |
| 5 | **A full rule run over one month completes in under 10 minutes.** Tight enough that a per-record design cannot pass. |

**Two defects the clarification pass exposed in the draft**, both fixed:

- `FR-010` assumed every finding points at a failing record. A missing feed has no record, and a
  volume deviation is a property of a period. Added `FR-010a` for aggregate-subject findings.
- `SC-001` promised "zero false positives" — but once the Medium-severity composite duplicate rule
  was accepted, a deliberately seeded namesake pair became an *expected* finding. `SC-001` now
  asserts set equality against an expected finding set.

That second one is the argument for never skipping `/speckit-clarify`: the contradiction was
invisible until a decision made it visible.

### Feature 1 — plan and design artifacts generated

`/speckit-plan` produced `plan.md`, `research.md`, `data-model.md`, `contracts/` (2 files),
`quickstart.md`.

**Central design decision:** a rule is a stored, versioned, parameterised SQL `SELECT` returning
failing subjects; the engine wraps it in one `INSERT … SELECT … ON CONFLICT DO NOTHING`. One
statement per rule, not per record — that is what makes the 10-minute target reachable across the
network link to Singapore. Idempotency becomes a `UNIQUE` constraint rather than application logic,
so it survives a run killed mid-flight.

**Constitution finding — a fifth role was needed.** The constitution names four roles
(`dq_readonly`, `dq_sandbox`, `dq_publish`, `dq_migrate`). None fits the deterministic rule runner,
which needs `SELECT` on `commercial` plus `INSERT` on `dq.finding`. Added **`dq_engine`**, recorded
in `plan.md` § Complexity Tracking. This extends principle VI rather than weakening it — the engine
is not an agent, holds no write access to `commercial`, and no role inherits another. No
constitution amendment required.

**Two deliberate absences of constraints in the schema**, both load-bearing:

- `sales_transaction` stores master-data references as **text, not foreign keys**. A FK would reject
  the orphaned row at insert, making FR-017 permanently undetectable.
- `hcp.npi` is **nullable and unvalidated**. Same reason for FR-015.

General principle recorded in `data-model.md`: *constraints protect referential truth; rules detect
data-quality defects.* Where they conflict, the rule wins — the defect must be able to land in the
table.

**Determinism is enforced at rule-registration time**, not by review: a predicate containing
`now()`, `random()`, or an unordered `LIMIT` is rejected before it can enter the registry. That is
what makes principle I structural. A predicate with `now()` would silently break FR-011 in a way no
ordinary test catches.

**Three risks flagged in `research.md`:** free-tier `t4g.nano` may not meet SC-008 at all; 500 MB
storage could be exhausted by test schemas plus volume data; and the predicate denylist is
incomplete by nature — mitigated by running each rule twice and asserting set equality, which
catches the class rather than enumerating members.

### Constitution stress-test — independent subagent review, plan FAILED the gate

An independent subagent (no memory of authoring the plan) audited all eleven principles against
`plan.md`, `research.md`, `data-model.md`, and both contracts. It found **five blocking
contradictions** and several structural weaknesses. The gate did its job — do not skip this step on
later features.

**Blocking — the design is not implementable as written:**

1. **A missing-feed finding has no batch to attach to.** `finding.batch_id` is `NOT NULL` and
   `run_rules(batch_id)` is batch-scoped, but FR-021d/SC-009 require reporting a feed that *never
   arrived*. `quickstart.md` Scenario 6 papered over it by running `FEED-MISSING` against an
   unrelated batch. Worse: `batch_id` is in the idempotency unique key, so the same missing period
   re-fires against every subsequent batch.
2. **The FR-019 overlap defect cannot be injected.** A GiST `EXCLUDE` constraint on
   `territory_alignment` makes overlaps uninsertable, and no staging table exists — so SC-001's
   "every injected defect is detected" cannot cover it. This also broke the schema's own stated rule
   (*constraints protect referential truth; rules detect defects*) two sections after stating it.
3. **Schema prefixing and `predicate_sql` cannot both work.** Predicates hard-code
   `commercial.hcp`; tests need `test_<uuid>_commercial`; and the contract forbids string
   interpolation. At most three of those four can hold.
4. **Master data has no current-version concept.** `hcp`/`product`/etc. carry only `batch_id`, so a
   monthly master re-delivery lands a second full copy — and FR-016a/FR-016b would flag the entire
   master file as duplicates of its own prior delivery. The two rules that motivated a whole
   clarification round cannot ship.
5. **Reproducibility is claimed but not delivered.** `research.md` D3 argued the predicate result
   set is fixed because batch and rule version are immutable. False: FR-017, FR-018, FR-020, FR-022
   all join to master data that is **not** batch-scoped and grows with every later delivery. SC-003
   is violated in production while every test passes.

**Structural weaknesses:**

- **The double-run determinism test cannot detect what it was built for.** `ON CONFLICT DO NOTHING`
  silently discards a second run's differing `offending_value` for the same `subject_key`, so the
  persisted set is identical *by construction*. The test must compare predicate output, not
  persisted findings.
- **`CURRENT_DATE` is missing from the non-determinism denylist** — along with `LOCALTIMESTAMP`,
  `statement_timestamp()`, and single-arg `age()`. `CURRENT_DATE` is the first thing a timeliness
  rule author would reach for. Fix: replace the denylist with a `pg_proc.provolatile = 'i'`
  allowlist over the parse tree, enforced by a `BEFORE INSERT` trigger on `rule_version`.
- **Roles are cluster-global**, which schema-prefixing structurally cannot isolate. A prefixed test
  migration either collides on `CREATE ROLE` or grants privileges on test schemas to the *shared dev
  roles*.
- **The stale-schema sweep is unimplementable as specified** — `pg_namespace` has no creation
  timestamp. Needs the timestamp embedded in the schema name or a registry table.
- **No `dq_ingest` or `dq_author` role.** Seeding writes `commercial` (only `dq_publish` can) and
  rule registration writes `dq.rule_version` (only `dq_migrate` can) — so Principle I's enforcement
  runs on a connection that can bypass it.
- **Summary double-counts across rule versions**, and SC-004's reconciliation check cannot catch it.
- **FR-014 is per-record in the spec, per-rule in the plan.** Set-based execution cannot report a
  per-record evaluation error.

**Governance:** the reviewer argued `dq_engine` needs a constitution amendment to v1.1.0, on the
grounds that Principle VI's four-role list includes *migrations* — not an agent — so
"those are the agent roles" does not hold. Accepted as the stronger reading. Their alternative is
better than the amendment alone: a CI test asserting `SELECT rolname FROM pg_roles WHERE rolname
LIKE 'dq\_%'` equals a list held in the constitution, so any sixth role fails the build until the
constitution is amended.

**What the review confirmed as sound:** the `dq_engine`-cannot-write-`commercial` grant plus a test
that attempts the write and asserts refusal; omitting the FK on `sales_transaction` and the
constraint on `hcp.npi` so defects can land; snapshotting `severity` onto `finding`;
`numeric(18,4)`; server-side `generate_series` for the volume test; and the honest labelling of
Principle VII as unenforced. The set-based one-statement-per-rule architecture is right — the
criticism is of premises it rests on, not the technique.

### Two decisions taken, then revision 2 of the design

**Decision 1 — master data becomes append-only version chains** (`valid_from`, natural key,
`is_deleted`, no `valid_to`), with predicates resolving master data as-of the period being
evaluated. Chosen because one change fixes three defects: duplicate rules flagging re-delivered
master records; reproducibility being false; and Feature 6's governed publish colliding with batch
immutability (a correction becomes a new version, not an `UPDATE`). The alternatives — an
`is_current` flag or a latest-batch view — are simpler but *mutable*, so a historical re-run sees a
different world and two of the three defects survive.

**Decision 2 — constitution amended to v1.1.0** rather than recording a deviation. Principle VI now
names all **seven** roles (`dq_migrate`, `dq_ingest`, `dq_author`, `dq_engine`, `dq_readonly`,
`dq_sandbox`, `dq_publish`), declares the list exhaustive, and requires a conformance test asserting
the database's `dq_*` roles equal that list. An eighth role fails the build until the constitution is
amended again — turning precedent erosion from a matter of restraint into a build failure.

### Revision 2 — all five blocking defects fixed

| Fix | Defect closed |
|---|---|
| Master version chains + `as_of_date` + `reference_watermark`, both recorded on `rule_run` | Duplicate rules flagged the whole master file; reproducibility was false in production while tests passed |
| `finding.batch_id` nullable, `scope_key` added, uniqueness rekeyed; runs take a **scope**, not a batch | A never-arrived feed had no batch to attach to, and aggregate findings re-fired per batch |
| GiST exclusion constraint removed from `territory_alignment` | FR-019's overlap defect was uninsertable, so SC-001 could not cover it |
| Predicates use **unqualified** table names; engine pins `search_path` per run | Schema prefixing, schema-qualified predicates, and no-interpolation were mutually exclusive |
| Determinism enforced by a `BEFORE INSERT` trigger requiring `provolatile = 'i'` | The denylist omitted `CURRENT_DATE` and ran only in application code that `dq_author` bypasses |

**Also fixed:** roles 5 → 7 with a bootstrap migration (roles are cluster-global and cannot be
schema-isolated); sweep now uses a timestamp embedded in the schema name (`pg_namespace` has no
creation time, so the original design was unimplementable); `COLLATE "C"` on composite-match
columns; immutability triggers extended to batch member rows and `finding`; `correlation_id` on
`rule_run`; `COMPLETED_WITH_ERRORS` status; summary counts per rule rather than per rule version;
`CHECK` giving SC-002 a mechanism; GUC pinning; advisory lock per scope+rule.

**Sharpest catch worth remembering:** the determinism test in revision 1 could never have failed.
It compared *persisted findings*, and `ON CONFLICT DO NOTHING` discards a second run's differing
row — so the stored set was identical by construction, not by determinism. It now compares predicate
output under varied query plans.

### Spec amended too — two requirements were unsatisfiable

- **FR-014 / Edge Cases:** error granularity is per rule, not per record. Set-based execution aborts
  the whole statement on one malformed value. Compensated by `COMPLETED_WITH_ERRORS` status and an
  explicit errored-rules line in the summary.
- **FR-003c / FR-003d added:** master data needs a time dimension, and historical re-evaluation must
  reproduce. Plus SC-010 to verify it, and assumptions covering generator-derived expected sets,
  composite-key uniqueness among non-defect HCPs, and a three-period seed.

### Feature 1 — tasks generated, then remediated by `/speckit-analyze`

`specs/001-data-foundation/tasks.md` — **83 tasks** across 7 phases, organised by user story.
(76 generated, 7 added during analyze remediation using suffixed IDs so nothing renumbered.)

| Phase | Tasks | |
|---|---|---|
| 1 Setup | T001–T008 | includes the storage-budget answer before anything depends on it |
| 2 Foundational | T009–T030 | schema, 7 roles, triggers, test harness — blocks all stories |
| 3 US1 detection | T031–T052 (+4 suffixed) | 🎯 MVP |
| 4 US2 rule governance | T053–T058 (+2 suffixed) | |
| 5 US3 reproducibility | T059–T064 | |
| 6 US4 query & summary | T065–T070 (+1 suffixed) | |
| 7 Polish | T071–T076 | |

Test tasks are included and mandatory — constitution v1.1.0 § Development Workflow requires contract
tests, integration tests, and a golden set for every feature.

**Ordering decisions that matter more than they look:**

- **T007 before T052.** T007 computes whether 1M rows plus indexes even fit in 500 MB. If the volume
  test is infeasible, that is a Phase 1 discovery, not a Phase 3 one.
- **T029/T030 before Phase 3.** They are the structural enforcement of principles II and VI. A user
  story built before they pass is built on an unverified foundation.
- **T050 before T033.** The expected finding set must be generated before the test asserting
  equality against it.
- **T064 sits in US3 but guards US1's rules.** A predicate written without binding
  `:reference_watermark` returns plausible results and passes every US1 test, then silently breaks
  historical reproducibility. Pull it forward if rule authoring starts before Phase 5.

**Largest parallel block:** T040–T048, the nine rule definitions — nine YAML files, no
interdependencies.

**MVP scope:** Phases 1–3. That is a shippable deterministic data-quality engine with zero agents —
exactly the trustworthy baseline the roadmap requires before agent work begins.

### `/speckit-analyze` — 0 critical, 97.9% coverage, 11 fixes applied

Cross-artifact consistency gate found **no CRITICAL issues** and one requirement with zero task
coverage. All findings above LOW were remediated.

**The one that mattered — C1 (HIGH):** `summarise_batch(batch_id)` filtered on `batch_id`, which is
**null for every aggregate finding**. A feed that never arrived would therefore appear in no summary
at all — visible only to someone who thought to query for it. That is the same class of defect the
plan review caught twice: passes every test, hides the most consequential real-world case. A steward
watching a pipeline whose numbers simply go quiet is exactly who this feature exists to protect.

Fixed by `summarise_scope(scope)` including `source_period` findings covering the scope's source and
period, plus FR-024a and a test (T066a).

**Other fixes:**

| Finding | Fix |
|---|---|
| I1 — batch/scope terminology drift in FR-009/011/012, SC-003 | Reworded to "scope" |
| C2 — FR-016d ("no fuzzy matching") unenforced | T025 now rejects `levenshtein`, `similarity`, pg_trgm operators by name — a deterministic `levenshtein(a,b) < 3` is IMMUTABLE and passed every other check. Negative test T031a |
| C3 — SC-007 untested | T055a, with a guard asserting no `src/dq/engine/` file changed |
| C4 — SC-009 untested | T033a |
| C5 — feed expectation catalogue unexercised | T049a seeds it, T055b tests end-dating |
| D1 — validation logic implemented twice | T035a asserts the Python validator and the DB trigger reject the same corpus |
| N2 — no "rule is wrong, not the data" seed scenario | Added to T050. Constitution X requires it before agents exist; Feature 1 is where seed data lives |
| I2 | `plan.md` said "eight rule families" in two places — now nine |
| I3 | SC-010 moved to the end of Success Criteria |
| A1 | T018 given a named verifying test |

Note I2 was mis-located in the analysis report — it was in `plan.md`, not `spec.md`; the spec uses
FRs rather than a bullet list.

**Verified after edits:** 83 tasks, no duplicate IDs, no malformed checklist lines, all seven new
task IDs present.

### Next

`/speckit-implement` — start with the MVP scope, Phases 1–3.

---

## Session 3 — 2026-08-24 — `/speckit-implement`, Phases 1–2 (T001–T030)

**Scope:** setup and foundation only. The stop condition was `alembic upgrade head` succeeding
against Supabase with the role conformance test passing. Phase 3 deliberately not started.

**Result: all 30 tasks complete.** 31 tests pass, mypy strict clean across 36 files, ruff clean.
The Supabase project now holds 7 roles, 5 schemas, 14 tables, 19 triggers and 3 functions, at
migration `0005`.

### What exists now

```
src/dq/config/{settings,models}.py      env loading, per-role URLs, model-ID placeholder
src/dq/db/{schemas,engine,migration_support}.py
src/dq/domain/{commercial,dq}.py        14 tables, symbolic schema names
migrations/versions/0001..0005          roles, schemas, tables, triggers, grants
tests/conftest.py                       prefixed schema lifecycle + stale sweep
tests/integration/                      conformance, privileges, predicate trigger
scripts/                                gen_role_passwords, db_state, db_sessions,
                                        env_check, measure_storage, fix_test_url
```

### T007 — the volume test fits, and indexes are why it nearly didn't

Measured, not estimated (`scripts/measure_storage.py`, 20k rows extrapolated):

| Table | bytes/row | heap | index | Projected |
|---|---|---|---|---|
| `sales_transaction` | 206.4 | 102.0 | 104.4 | 196.9 MB @ 1M |
| `hcp` | 338.3 | 111.0 | 227.3 | 32.3 MB @ 100K |
| | | | **Total** | **229.1 MB** |

271 MB headroom against the 500 MB ceiling, so T052 is buildable and R1's storage half is closed.
**Indexes are 51% of `sales_transaction` and 67% of `hcp`** — a column-width estimate would have
been out by 3x and called this infeasible. Recorded in `research.md` § D8.

### Six failures worth remembering

Each cost real time and none was visible in the design.

**1. Silent rollback that reported success.** `alembic upgrade head` printed both migrations and
exited 0, with nothing committed. The pre-flight probe in `env.py` opened an implicit transaction;
Alembic then nested inside it rather than owning it, and `engine.connect()` rolled everything back
on exit. The fix is one unconditional `connection.commit()` after the probe. **Exit code 0 is not
evidence that a migration applied** — `scripts/db_state.py` exists because of this.

**2. An orphaned backend blocked everything.** A run the pooler dropped left a backend
`idle in transaction` for 16 minutes, holding catalog locks. Unrelated `CREATE FUNCTION` and
`GRANT` statements then died on `statement_timeout` with messages naming `pg_proc` and
`pg_database` — which read like permission or corruption problems, not lock waits.
`scripts/db_sessions.py --kill-idle` diagnoses and clears it. Expect this after any killed run.

**3. `GRANT CREATE ON DATABASE` is not usable here.** It updates a contended row in the
cluster-wide `pg_database` catalog and dies on timeout, taking the connection with it. Removed —
`dq_migrate` owns all five schemas, so nothing in Feature 1 or 2 needs it. **The first feature that
adds a sixth schema must run that one GRANT out of band, as the Supabase owner, and should expect
to retry it.** Noted in migration 0005.

**4. `%` in PL/pgSQL cannot travel through SQLAlchemy.** `text()` escapes every `%` to `%%` for
psycopg's pyformat paramstyle; `exec_driver_sql` still trips psycopg's placeholder parser
(`only '%s', '%b', '%t' are allowed`). `RAISE` placeholders and `format()` specifiers are full of
them. `migration_support.execute_raw()` goes to the driver cursor with no `params`, skipping
placeholder parsing entirely.

**5. `pg_depend` cannot see built-in functions.** The T025 trigger originally read dependency rows
to learn which functions a predicate resolved. **PostgreSQL records no dependencies on pinned
system objects** — so it saw `levenshtein` (an extension function) and was blind to `now()` and
`random()`, which are exactly the ones principle I is about. Rewritten to scan
`pg_rewrite.ev_action`, the actual parse tree, for `:funcid` / `:opfuncid` / `:relid`. The same fix
applies to rule 5 and `pg_catalog` tables.

`CURRENT_DATE` then needed a third mechanism: it parses to a `SQLValueFunction` node, so it carries
no function OID *and* no parentheses. It is caught lexically or not at all — and it is the first
thing a timeliness-rule author writes.

**6. The cast operator and the parameter marker share a character.** `h.hcp_id::text` reads as a
parameter named `:text`. This broke both rule 8 and the probe substitution, rejecting every
well-formed predicate. Casts are now stripped (rule 8) or sentinel-protected (probe) first.

### Decisions taken during implementation

| Decision | Why |
|---|---|
| Symbolic schema names + `schema_translate_map` | One predicate string runs against `commercial` and against a prefixed test schema with no rewriting. Raw DDL uses `schemas.physical()`, validated by `assert_safe_identifier` |
| T025 validates by building a **temp view** and reading its parse tree | Exact rather than lexical: it sees through aliases, operators, and overload selection. `date_trunc(text, timestamp)` passes as IMMUTABLE while the `timestamptz` overload is rejected — no name-based check can make that distinction |
| That function is `SECURITY DEFINER` | `dq_author` has no SELECT on `commercial` (T030 asserts it) and could not otherwise plan a predicate over it. Creating a view does not execute the query, so no data can leak through it |
| Rule 6 (total ordering) is an approximation | It checks that the final `ORDER BY` term names a known-unique column. A real proof needs the constraint set. Deliberately strict — in the duplicate families, ties are the whole subject matter |
| `env.py` probes for `dq_migrate` rather than guessing | The first upgrade must run as the superuser because 0001 creates that role; every later one runs as `dq_migrate`. One extra round trip removes a bootstrap step that is easy to get wrong |
| The conformance test parses the constitution | A test carrying its own copy of the seven roles would keep passing after someone edited principle VI |
| Passwords generated into `.env`, never printed | `scripts/gen_role_passwords.py`. It also derives the seven `DQ_*_URL` from the stateful URL, so no connection string is ever pasted anywhere |

### Environment fixes

- `TEST_DATABASE_URL` and `SUPABASE_DB_POOLED_URL` held **bare hostnames, not URLs**. The first
  blocked every integration test; it now mirrors `SUPABASE_DB_STATEFUL_URL` verbatim
  (`scripts/fix_test_url.py`). **`SUPABASE_DB_POOLED_URL` is still malformed** — unused in Feature
  1, but it will fail the moment something reads it.
- `.env.bak` now exists (a backup is taken before each rewrite). Gitignored; delete when happy.
- `ANTHROPIC_API_KEY` is unset. Not needed until Feature 3.

### Known limitations, carried forward

1. **Rule 6 is approximate** (above). Tighten it if a legitimate rule is ever wrongly rejected.
2. **No `CREATE` on database for `dq_migrate`** (above).
3. **A `:word` inside a string literal** is substituted during probe construction. It can only turn
   a valid predicate into an unplannable one, which surfaces as a clear error rather than as a
   wrong verdict.
4. **Principle VII is still unenforced**, as designed. Synthetic data only, and no masking
   mechanism because no prompt boundary exists yet. Feature 3.
5. **T052's compute half is untested.** Storage fits; whether `t4g.nano` runs 1M rows in under 10
   minutes is unknown until T052 actually runs.

### Next

Phase 3 (US1), T031–T052 — the MVP. It ends with SC-001 set equality and T052.
Run T052 *early*, not last: it is the check that catches an architecturally wrong design, and its
whole value lies in catching that before three more stories are built on top.

```powershell
uv run pytest                 # 31 pass
uv run alembic upgrade head   # at 0005
uv run python scripts/db_state.py
```
