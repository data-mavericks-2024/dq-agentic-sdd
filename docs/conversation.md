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
