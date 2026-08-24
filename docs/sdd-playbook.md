# SDD Playbook — Agentic Data Quality Monitoring & Resolution Platform

**Stack:** Claude Code + GitHub Spec Kit (SDD) + LangGraph + Supabase (PostgreSQL) + FastAPI + Streamlit + Claude API
**Created:** 2026-08-24
**Stack locked:** 2026-08-24
**Environment constraint:** No Docker on the development machine. All PostgreSQL is Supabase-hosted, there is no
local database and no `supabase start`, and the test strategy must not depend on containers.

> Copy this file into your repo as `docs/sdd-playbook.md` once you run `specify init`.

---

## Use case (reference)

A pharmaceutical commercial-data team receives sales, HCP/HCO, product, and territory-alignment
data from multiple sources. Data-quality problems are normally detected through fixed rules, but
analysts still spend significant time investigating root causes, assessing business impact, and
deciding how to correct the data.

The **Agentic Data Quality Monitoring & Resolution Dashboard** is a PostgreSQL-based solution that
combines deterministic data-quality controls with autonomous, tool-using AI agents. It detects and
prioritizes commercial-data issues, investigates root causes using supporting evidence, assesses
downstream business impact, recommends and validates safe corrections, and obtains human approval
before publishing corrected data.

**Business value:** reduce manual investigation effort, accelerate issue resolution, improve
reliability of commercial reporting, and provide a transparent, governed decision trail.

---

## Locked stack decisions

These are settled. `/speckit-specify` still never mentions them; `/speckit-plan` always does.

| Concern | Decision | Consequence for the specs |
|---|---|---|
| Datastore | **Supabase** (managed PostgreSQL) — operational, quality, workflow, and audit data all in one project | Single system of record holds commercial data, DQ metadata, LangGraph checkpoints, and the audit trail |
| Agent orchestration | **LangGraph**, `PostgresSaver` checkpointer in the same Supabase database | Durable interrupts for human approval; agent state and business data share a backup policy |
| Detection | **SQL-based rules** executed in PostgreSQL | Detection stays deterministic; agents never decide pass/fail |
| Reasoning | **Claude** via the Anthropic API | Investigation, impact narration, remediation proposals |
| Backend APIs | **FastAPI** | Owns every state-changing operation and every privileged credential |
| UI | **Streamlit** | Pure client of FastAPI; holds no database credential and issues no SQL |
| Migrations | **Alembic** (not the Supabase dashboard) | Schema is code-reviewed and reproducible across dev and test projects |
| Tests | Dedicated Supabase **test project**, schema-per-run isolation | `testcontainers` is unavailable — no Docker on the dev machine |

---

## Phase 0 — Bootstrap (terminal commands, not prompts)

```
uvx --from git+https://github.com/github/spec-kit.git specify init --here --integration claude --script ps
claude
```

`--here` initializes into the current directory rather than creating a nested one, so this playbook
ends up inside the repo. `--script ps` selects PowerShell helper scripts. The flag is `--integration`,
not `--ai` — that was renamed in Spec Kit; run `specify init --help` through `uvx` if it changes again.
Add `--force` to skip the non-empty-directory confirmation.

Then, before any spec work, wire the environment. **Prompt:**

```
Read the repo layout created by Spec Kit. Then set up this project's working environment WITHOUT writing application code:

1. Create/extend CLAUDE.md documenting the stack: Python 3.12; uv for dependency management; LangGraph as the agent framework with PostgresSaver as the checkpointer; Supabase (managed PostgreSQL) as the single system of record for commercial data, DQ metadata, workflow state, and audit; FastAPI for backend APIs; Streamlit for the steward-facing UI; Claude via the Anthropic API for investigation and explanation; pytest, ruff, mypy strict.

2. Record these Supabase-specific working rules in CLAUDE.md, because violating them causes silent runtime failures:
   - Alembic, the LangGraph checkpointer, and anything using prepared statements or advisory locks MUST use the DIRECT (non-pooled) connection string. Transaction-mode connection pooling breaks both.
   - The Supabase service-role key is never referenced from agent code, from FastAPI request handlers, or from Streamlit. Agents connect under dedicated least-privilege database roles.
   - Schema is owned by Alembic migrations. No schema change is ever made through the Supabase dashboard.
   - There is no local Postgres and no Docker on this machine. Do not propose testcontainers, docker-compose, or `supabase start`.

3. Add a .claude/settings.json permission allowlist for: uv, pytest, ruff, mypy, alembic, uvicorn, streamlit, and psql restricted to the read-only role.

4. Propose (do not install) an MCP server config for Supabase/Postgres pointed at the read-only role, so schemas can be explored interactively during development.

5. List what you need from me as environment variables/secrets. Distinguish the direct connection string from the pooled one, and separate dev from test.

Do not create any source files yet.
```

---

## Phase 1 — Constitution

The highest-leverage prompt in the flow. Everything downstream inherits it.

```
/speckit-constitution Create the governing principles for an agentic data-quality platform in a regulated pharmaceutical commercial-data environment.

Non-negotiable principles:

1. DETERMINISM FIRST — Detection of data-quality failures is always deterministic (SQL rules in PostgreSQL). Agents never decide *whether* a record failed; they only investigate, explain, assess impact, and propose. Any agent output that contradicts a deterministic rule result is a defect.

2. NO AUTONOMOUS WRITES — No agent may mutate curated/published commercial data. Agents may only write to proposal, evidence, and audit tables. Every correction to production data passes through an explicit human-steward approval gate.

3. EVIDENCE-BOUND REASONING — Every root-cause hypothesis, impact assessment, and remediation recommendation must cite persisted evidence rows (query results, lineage records, historical comparisons) by ID. Unciteable claims are rejected before reaching a steward.

4. SIMULATE BEFORE PROPOSE — Every proposed correction must be executed against an isolated sandbox schema first, and must report: rows affected, rules that flip from fail to pass, rules that regress, and referential-integrity effects. A proposal without a validation result cannot be surfaced for approval.

5. FULL AUDITABILITY — Every agent step (input state, tool call, tool result, model, prompt version, token cost, latency, decision) is persisted to an append-only audit table with a correlation ID. The trail must be reconstructable end-to-end for any published correction, for audit/inspection purposes.

6. LEAST-PRIVILEGE DATA ACCESS — Investigation agents use a read-only database role. Remediation runs under a sandbox-write role. Publishing runs under a separate role invoked only after approval. No role inherits the privileges of another. The Supabase service-role key and any superuser credential are never available to agent code, to request handlers serving investigation reads, or to the UI process; they exist only in the migration tooling's environment.

7. PII/PHI DISCIPLINE — HCP identifiers and any patient-adjacent fields are masked or tokenized before entering an LLM prompt. Prompt payloads are logged with masking applied.

8. DETERMINISTIC ORCHESTRATION — Control flow lives in the LangGraph graph (explicit nodes and conditional edges), not in model free-choice. Agent autonomy is scoped to bounded ReAct loops inside individual nodes with hard iteration and cost caps.

9. RESUMABILITY — Every workflow is checkpointed. Human approval is implemented as a durable graph interrupt that can be resumed hours or days later without state loss.

10. TESTABILITY — Rules, tools, and graph nodes are independently testable. Agent behavior is tested against a golden set of seeded, known-root-cause data-quality scenarios with expected hypotheses and expected recommendations.

11. THE UI IS NOT A TRUST BOUNDARY — The user interface renders state and captures steward intent. It never writes to curated data, never holds a privileged credential, and never enforces an approval rule on its own. Every state-changing action travels through a backend endpoint that independently re-validates steward authority, proposal freshness, and constitution compliance server-side. A control that is merely hidden in the UI is not a control.

Quality bars: every feature ships with contract tests for tools, integration tests against a seeded Postgres, and a documented rollback path. Structured outputs are schema-validated (Pydantic) at every agent boundary.
```

---

## Phase 2 — Slice the product into SDD features

Spec Kit works one feature per branch. Don't spec the whole platform at once.
Run as a **normal prompt** (not a slash command):

```
Read .specify/memory/constitution.md. I want to build the Agentic Data Quality Monitoring & Resolution platform described below.

<paste your full use-case description here>

Do NOT write specs or code. Produce a feature roadmap that slices this into 7 independently specifiable, independently shippable features, ordered by dependency. For each feature give: name, one-paragraph scope, explicit out-of-scope list, the user-visible outcome, and its dependency on prior features. Write the result to docs/roadmap.md.

Constraints:
- Feature 1 must deliver value with zero agents (deterministic rules only), so we have a trustworthy baseline to measure agent contribution against.
- Feature 6 (human approval) may include a minimal approval surface, since an approval gate with no way to approve is not shippable. The full dashboard stays in Feature 7. State explicitly where you draw that line.
```

### Target feature slices

| # | Feature | Core content |
|---|---|---|
| 1 | **Data foundation & deterministic rule engine** | Commercial schema (sales, HCP, HCO, product, territory alignment), DQ metadata schema, rule registry, SQL rule runner, seeded synthetic data |
| 2 | **Issue triage & prioritization** | Finding→issue clustering, severity/priority scoring, first LangGraph graph + checkpointer |
| 3 | **Root-cause investigation agent** | Read-only Postgres toolset, hypothesis generation + ranking, evidence persistence |
| 4 | **Business impact assessment agent** | Lineage, affected KPIs/reports/territories, quantified downstream blast radius |
| 5 | **Remediation recommendation & sandbox validation** | Correction proposal generation, sandbox-schema simulation, safety gates, regression detection |
| 6 | **Human-in-the-loop approval & governed publish** | LangGraph `interrupt`, FastAPI approval endpoints, steward decision capture, publish under privileged role, immutable audit trail, minimal Streamlit approval page |
| 7 | **Dashboard, API surface & observability** | Full Streamlit steward dashboard over the FastAPI service layer, agent trace views, LangSmith/OTel instrumentation, cost + latency SLOs |

> Features 1–5 are backend-only. FastAPI appears first in Feature 6 (because the approval gate must be
> server-enforced per principle 11), and Streamlit appears there only as the thinnest possible page.
> Resist pulling UI work forward — it is the easiest way to accidentally put a trust decision in the client.

---

## Phase 3 — The per-feature cycle

Run this six-step loop for **each** feature. Spec Kit also installs three optional skills —
`/speckit-clarify` (used below as step 3.2), `/speckit-checklist` (requirements-quality checklist, run
after `/speckit-plan`), and `/speckit-analyze` (used below as step 3.5). There is also
`/speckit-converge`, which assesses an existing codebase and appends remaining work as tasks; it is
useful mid-feature when implementation has drifted from `tasks.md`, not as part of the standard loop.

### Step 3.1 — `/speckit-specify` (WHAT and WHY, never HOW)

#### Feature 1 — Deterministic foundation

```
/speckit-specify Deterministic data-quality foundation for pharmaceutical commercial data.

WHAT: A data foundation plus a deterministic rule engine that detects data-quality failures across four commercial domains: sales transactions, HCP master, HCO master, product master, and territory alignment.

Users: data engineers (configure rules), data stewards (see failures).

Capabilities:
- Represent the four commercial domains plus territory-to-HCP alignment with effective-dated assignments.
- A versioned rule registry where each rule declares: domain, dimension (completeness, uniqueness, validity, referential integrity, timeliness, consistency, conformity), severity, owning business function, and a deterministic pass/fail predicate.
- A rule runner that executes all active rules for a given data batch and persists one finding per failing record with the offending value and rule version.
- Rule execution is idempotent and re-runnable for any historical batch.
- Findings are queryable by domain, rule, severity, batch, and time window.

Representative rule families the system must support (used as acceptance scenarios):
- HCP records missing NPI or with structurally invalid NPI
- Duplicate HCP records under distinct surrogate keys
- Sales transactions referencing a product or HCP not present in master data
- Sales attributed to a territory with no active alignment for the transaction date
- Overlapping or gapped territory-alignment effective date ranges
- Unit-of-measure inconsistency between sales and product master
- Late-arriving or missing expected data feed for a source/period
- Period-over-period volume deviation beyond a configured threshold

OUT OF SCOPE for this feature: any AI agent, any LLM call, any remediation, any user interface, any HTTP API.

Acceptance: given a seeded dataset containing deliberately injected defects of each family above, a full rule run detects exactly the injected defects with no false positives, and produces a per-batch summary of failures by domain, rule, and severity.
```

#### Feature 3 — Root-cause investigation agent (the trickiest to spec)

```
/speckit-specify Root-cause investigation agent for prioritized data-quality issues.

WHAT: For a prioritized data-quality issue, an autonomous tool-using agent investigates why the failure occurred, gathers supporting evidence from the database, and produces a ranked set of root-cause hypotheses with confidence and citations.

Users: data stewards and data engineers who currently do this investigation manually.

Capabilities:
- Accept an issue (a cluster of related findings) and produce an investigation report.
- Autonomously choose and sequence read-only investigative actions such as: profiling the failing column, comparing against the prior N loads, inspecting the source-system and load-batch metadata for the affected records, checking whether master-data records exist under alternate keys, checking alignment effective dating around the transaction date, and checking whether the same pattern appeared historically and how it was previously resolved.
- Produce 1 to 5 ranked hypotheses. Each hypothesis states the causal mechanism, a confidence score, the evidence IDs supporting it, and the evidence that would confirm or refute it.
- Explicitly distinguish source-system defects, ingestion/transformation defects, timing/late-arrival effects, legitimate business change, and rule misconfiguration (a rule that is itself wrong).
- Persist the full investigation trail: every tool call, its arguments, its result reference, and the reasoning step that motivated it.
- Terminate deterministically: bounded tool-call count, bounded wall-clock, bounded token cost, with a partial-result report on exhaustion rather than a failure.

OUT OF SCOPE: proposing or applying any correction, assessing business impact, human approval, UI.

Acceptance scenarios (golden set): for each seeded scenario with a known planted root cause — (a) an HCP duplicate created by a source-system merge, (b) sales orphaned by a product retired in master but still transacting, (c) alignment gap caused by a territory realignment effective date, (d) a volume drop caused by a genuinely missing feed file, (e) a volume drop that is legitimate seasonality and is NOT a data defect, (f) a failure caused by a stale rule threshold — the agent's top-ranked hypothesis matches the planted cause, and every hypothesis cites at least one persisted evidence record.

Note scenario (e) and (f) specifically: the agent must be able to conclude "the data is correct, the alert is not actionable" and "the rule is wrong, not the data."

Non-functional: investigation completes in under 3 minutes p95 for an issue of up to 10,000 findings, measured against a managed database reached over the network rather than a local one; the agent never issues a write statement.
```

> Use the same shape for Features 2, 4, 5, 6, 7 — always:
> **WHAT / users / capabilities / explicit OUT OF SCOPE / acceptance scenarios / non-functional.**
> Never name LangGraph, Supabase, FastAPI, Streamlit, tables, or libraries in `/speckit-specify` — that's `/speckit-plan`'s job.
> "The database" and "the user interface" are the right level of abstraction for a spec.

---

### Step 3.2 — `/speckit-clarify` (do not skip)

Spec Kit labels this one optional. For this project it is not.

```
/speckit-clarify
```

Then push further:

```
Beyond the questions you already asked, list every remaining ambiguity in this spec that could cause two competent engineers to build materially different systems. For each: state the ambiguity, the options, the trade-off, and your recommendation. Focus specifically on: what constitutes "the same issue" for clustering, how confidence scores are calibrated and what they mean, what happens when evidence contradicts itself, and what the agent does when it has no viable hypothesis. Update the spec with resolutions once I confirm.
```

---

### Step 3.3 — `/speckit-plan` (HOW — the stack enters here)

```
/speckit-plan Implement using this technical stack and architecture.

Stack: Python 3.12, uv, LangGraph for orchestration, LangChain tool bindings only where needed, Pydantic v2 for all structured outputs, Supabase (managed PostgreSQL) as the sole datastore, SQLAlchemy Core + Alembic for schema, FastAPI for backend APIs, Streamlit for the steward UI, pytest for tests, ruff + mypy strict.

Model: Claude via the Anthropic API using the official `anthropic` Python SDK. Use `claude-sonnet-5` for investigation/impact/remediation reasoning nodes and `claude-haiku-4-5` for cheap classification and summarization nodes. Pin model IDs in a single config module and never inline them; the reasoning tier must be swappable to `claude-opus-5` by config change alone. Use adaptive thinking (`thinking={"type": "adaptive"}`) on reasoning nodes, and stream any call with a large max_tokens so long investigations do not hit HTTP timeouts.

Supabase specifics — treat these as hard constraints, not preferences:
- Two Supabase projects: dev and test. There is NO local Postgres, NO Docker, and NO `supabase start` available on the development machine. Do not propose any design that requires them.
- All schema is owned by Alembic migrations. Schema is never changed through the Supabase dashboard. Migrations run under a dedicated migration role.
- Use the DIRECT (non-pooled) connection string for Alembic, for the LangGraph PostgresSaver, and for anything relying on prepared statements or advisory locks. Transaction-mode pooling breaks these. The pooled endpoint may be used only for short read-only queries; document exactly which code paths use which.
- Define four database roles with explicit, non-inherited grants: `dq_readonly` (investigation), `dq_sandbox` (write access to the sandbox schema only), `dq_publish` (write access to curated commercial tables, reachable only after approval), and `dq_migrate` (Alembic). Show the GRANT statements in data-model.md.
- The service-role key and any superuser credential appear only in the migration environment. Prove structurally that agent code cannot reach them.
- Decide and document whether Row Level Security is enabled on the curated schema, and if so how the server-side roles interact with it. If you disable RLS, say why and what compensates.
- Schema layout: separate schemas for curated commercial data, DQ metadata, agent/workflow state, audit, and the remediation sandbox. Say which role can touch which schema.

Testing strategy — `testcontainers` is NOT available:
- Integration tests run against the dedicated Supabase test project.
- Each test session creates a uniquely named schema, applies Alembic migrations into it, and drops it on teardown, so concurrent runs and CI cannot collide.
- Unit tests for rules, tools, and graph nodes run against a stubbed model with no network access.
- Golden-scenario tests are the only tests permitted to call a real model, and they must be separately markable so the default test command is fast and free.
- Quantify the network round-trip cost this imposes and state the CI time budget.

LangGraph architecture:
- One top-level StateGraph per issue lifecycle, with typed state (Pydantic/TypedDict) carrying: issue ID, findings summary, evidence refs, hypotheses, impact assessment, proposals, validation results, approval decision, audit correlation ID.
- Each capability (triage, investigate, assess impact, recommend, validate) is a SUBGRAPH, so it is independently testable and independently resumable.
- Agentic nodes are bounded ReAct loops using LangGraph's prebuilt agent pattern, with explicit recursion_limit and a cost guard.
- Control flow between capabilities uses explicit conditional edges, not model choice.
- PostgresSaver as the checkpointer, in the same Supabase database, so agent state and business data share a transaction boundary and a backup policy.
- The LangGraph Store holds cross-issue memory: previously confirmed root causes and their resolutions, keyed by rule + domain + pattern signature, so the investigator can retrieve precedent.
- Human approval is a durable `interrupt` at the approval node; resumption happens via a thread ID resolved from the approval record.
- Use `Command` for state updates + routing from agentic nodes.

API and UI architecture:
- FastAPI owns every state-changing operation: triggering a rule run, starting an investigation, approving or rejecting a proposal, and resuming an interrupted graph. Each endpoint independently re-validates steward authority, proposal freshness (the underlying data must not have changed since simulation), and constitution compliance — server-side, every time.
- Streamlit is a pure client of the FastAPI service. It holds no database credential, issues no SQL, and imports no agent code.
- Resuming a LangGraph interrupt is a FastAPI endpoint that resolves the thread ID from the approval record. Streamlit only posts the steward's decision and polls for the outcome.
- Rule runs and investigations are started asynchronously and polled. No UI interaction blocks on a multi-minute agent run. Specify where the async work actually executes and how a crashed worker is recovered from the checkpoint.
- Specify how a steward is authenticated and how their identity reaches the audit trail.

Tool design: every agent tool is a plain Python function with a Pydantic-validated signature, executing parameterized SQL against a read-only role. No free-form SQL string from the model reaches the database — if dynamic querying is required, expose a constrained query builder with an allowlist of tables, columns, and predicates, and validate before execution.

Produce: data-model.md (full DDL design for the commercial, DQ metadata, workflow, audit, and sandbox schemas, plus role grants), contracts/ (tool signatures, state schemas, and the FastAPI OpenAPI contract), research.md (decisions and rejected alternatives), and quickstart.md.

Explicitly document in research.md: how you enforce constitution principles 2, 3, 4, 6, and 11 structurally rather than by prompt instruction; why the sandbox-schema approach was chosen over alternatives for principle 4; and what the no-Docker constraint cost you in the test strategy.
```

Then stress-test before generating tasks:

```
Review the plan you just produced against .specify/memory/constitution.md. For each of the 11 principles, cite the specific design element that enforces it, or flag it as unenforced. Pay particular attention to principle 11: show me the code path by which a Streamlit action becomes a curated-data write, and name every place it is re-validated.

Then list the three largest technical risks in this plan and what would have to be true for each to become a real problem.
```

---

### Step 3.4 — `/speckit-tasks`

```
/speckit-tasks
```

Then tighten:

```
Review the generated tasks.md. Enforce these rules and rewrite it:
- Every task is completable and verifiable in isolation, with an explicit "done when" that names a test or a runnable command.
- Test tasks precede implementation tasks for the same unit.
- Tasks that touch disjoint files are marked [P] for parallel execution.
- No task depends on an LLM being available except the ones explicitly testing agent behavior; everything else must be testable with a stubbed model.
- Add an explicit task for the Supabase test-schema lifecycle (create uniquely named schema, apply migrations, drop on teardown) before any integration-test task depends on it.
- Add an explicit task for the four database roles and their grants before any task that connects under one of them.
- Add an explicit task for the golden-scenario seed data before any agent task.
- Flag any task that requires a decision I have not yet made.
```

---

### Step 3.5 — `/speckit-analyze` (cross-artifact consistency gate)

```
/speckit-analyze
```

Then:

```
Report any requirement in spec.md that has no corresponding task, any task with no requirement, and any place where plan.md contradicts the constitution. Do not fix anything yet — give me the discrepancy list first.
```

---

### Step 3.6 — `/speckit-implement`

```
/speckit-implement
```

For a large feature, run in bounded chunks instead:

```
/speckit-implement Execute only tasks T001 through T0NN from tasks.md. After each task, run the associated tests and stop immediately if any fail. Do not proceed past a failing test. Report status per task.
```

Post-implementation verification:

```
For the feature just implemented, prove it works without me reading the code:
1. Run the full test suite and show me the output.
2. Run the seeded golden scenarios end to end and show the actual agent outputs against the expected outputs from spec.md.
3. Show me one complete audit trail for a single issue, from detection through the final node.
4. Show me that no code path outside the migration tooling references the Supabase service-role key, and that Streamlit holds no database credential.
5. Tell me plainly what in this feature is NOT yet working, incomplete, or stubbed.
```

---

## Phase 4 — Supporting prompts worth running once

### Persistent Claude Code assets (after Feature 2)

```
Create .claude/agents/ subagents for this project, each with a tightly scoped tool list:
- dq-rule-author: writes and reviews deterministic SQL rules for the rule registry; read-only DB tools plus file write to the rules directory.
- langgraph-reviewer: reviews graph topology, state schema changes, and checkpointer usage for constitution compliance; read-only.
- pharma-data-reviewer: reviews changes for PII/PHI exposure in prompts and logs, and for commercial-data semantic errors; read-only.
- boundary-reviewer: reviews any change to the Streamlit or FastAPI layer for principle 11 violations — a privileged credential in the UI process, SQL issued from Streamlit, or a state-changing action that is not re-validated server-side; read-only.

Also create a PreToolUse hook that blocks any Edit/Write that introduces:
(a) a raw SQL INSERT/UPDATE/DELETE against the curated commercial schema outside the designated publish module,
(b) a reference to the Supabase service-role key anywhere outside the migration tooling, or
(c) a database connection string or SQL statement inside the Streamlit application package.
```

### Evaluation harness (after Feature 3)

```
Design (do not implement) an evaluation harness for agent quality: root-cause top-1 and top-3 accuracy against the golden set, false-actionable rate on the legitimate-business-change scenarios, evidence-citation completeness, recommendation safety rate, per-issue token cost, and p95 latency. Specify how each metric is computed, what the baseline is, and what regression threshold should fail CI. Write it to docs/evaluation.md.
```

### Threat model (before Feature 6 — the riskiest)

```
Before we spec the human-approval and publish feature, write docs/threat-model.md covering: prompt injection via data values (an HCP name field containing instructions), an agent proposing a correction that silently widens its own blast radius, approval replay/resume after the underlying data has changed, steward approving a stale proposal, privilege escalation between the read-only, sandbox, and publish roles, and a forged or replayed approval request reaching the backend from something other than the real UI. For each, the structural control that prevents it.
```

---

## The discipline that makes this work

1. **One feature = one branch = one spec.** Spec Kit creates the branch; don't merge features into one spec.
2. **Never skip `/speckit-clarify`.** Spec Kit marks it optional; in this domain, spec ambiguity becomes wrong agent behavior that looks plausible.
3. **`/speckit-specify` contains zero technology.** The moment "LangGraph", "Supabase", or "Streamlit" appears in a spec, the spec has become a plan.
4. **The constitution is a living gate.** Re-run the compliance prompt at each `/speckit-plan` and each post-implementation check.
5. **Build the golden scenarios before the agents.** Scenarios (e) legitimate change and (f) wrong rule are what separate this from a dashboard. An agent that can't say "no action needed" will erode steward trust fastest.
6. **The UI is never the gate.** Streamlit makes it trivially easy to open a database connection and write a row. Every time you are tempted, the answer is a FastAPI endpoint that re-validates server-side.

---

## Quick command reference

| Step | Command | Output artifact |
|---|---|---|
| 0 | `specify init --here --integration claude` | repo scaffold |
| 1 | `/speckit-constitution` | `.specify/memory/constitution.md` |
| 2 | plain prompt | `docs/roadmap.md` |
| 3.1 | `/speckit-specify` | `specs/NNN-feature/spec.md` (+ new branch) |
| 3.2 | `/speckit-clarify` | updated `spec.md` |
| 3.3 | `/speckit-plan` | `plan.md`, `data-model.md`, `research.md`, `contracts/`, `quickstart.md` |
| — | `/speckit-checklist` | optional requirements-quality checklist |
| 3.4 | `/speckit-tasks` | `tasks.md` |
| 3.5 | `/speckit-analyze` | consistency report |
| 3.6 | `/speckit-implement` | working code + tests |
| — | `/speckit-converge` | appends remaining work as tasks when code has drifted from `tasks.md` |

---

## Open items

- [ ] Create the two Supabase projects (dev, test) and capture both direct and pooled connection strings
- [ ] Decide whether Row Level Security is enabled on the curated schema, and what compensates if not
- [ ] Decide how stewards authenticate to Streamlit, and how that identity reaches the audit trail
- [ ] Decide where async agent runs execute (FastAPI background task vs. a separate worker process) and how a crashed run is recovered from its checkpoint
- [ ] Draft full `/specify` prompts for Features 2, 4, 5, 6, 7
- [ ] Confirm source systems and real feed cadence for the timeliness rules
- [ ] Identify the actual downstream reports/KPIs for Feature 4 lineage