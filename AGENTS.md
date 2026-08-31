# Codex project instructions

## Project

This repository implements an agentic data-quality monitoring and resolution platform for
pharmaceutical commercial data. Deterministic SQL rules decide whether records fail. Agents may
investigate, assess impact, and propose corrections, but they never decide failure status and never
write directly to curated data. Human steward approval is required before publishing.

## Required context

Before planning, auditing, or implementing:

1. Read `.specify/memory/constitution.md`; its eleven principles are non-negotiable and take
   precedence over every other project artifact.
2. Read the active feature artifacts under `specs/001-data-foundation/`, including `spec.md`,
   `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `contracts/`, and `quickstart.md` as
   relevant to the task.
3. Read `CLAUDE.md` for the detailed stack, Supabase, security, testing, environment, and coding
   rules. Those rules apply to Codex too, subject to the corrections below.
4. Read `docs/sdd-playbook.md` when planning a feature or deciding workflow/roadmap scope.

## Current handoff state

- Application code and tests already exist. The statement in `CLAUDE.md` that no application code
  exists is stale and must be ignored.
- The active branch and feature are `001-data-foundation`.
- Tasks T001-T064 in `specs/001-data-foundation/tasks.md` were implemented with Claude Code and are
  recorded as complete.
- Remaining planned work begins at T065.
- Treat existing code as an implementation candidate: verify it against the constitution, specs,
  contracts, and tests. Do not rewrite working code for stylistic preference.
- Reopen a completed task only when there is concrete code or test evidence of a gap.
- Do not regenerate the specification, plan, or task list unless requirements actually changed.

## Spec Kit workflow in Codex

Spec Kit is installed as project skills under `.agents/skills`. In Codex use `$speckit-*` skill
names, not the `/speckit-*` names shown in `CLAUDE.md`.

For this partial implementation, use this order:

1. `$speckit-converge` to compare existing code with the artifacts and append genuine gaps.
2. `$speckit-analyze` to check cross-artifact consistency before further implementation.
3. `$speckit-implement` to continue from the first verified unchecked task.

Do not start again with `$speckit-specify`, `$speckit-plan`, or `$speckit-tasks` for
`001-data-foundation` unless the user changes requirements.

## Working agreements

- Preserve user changes and existing Git history. Inspect `git status` and relevant diffs before
  editing.
- Never expose or commit values from `.env` or `.env.bak`.
- Follow the test-first requirements and exact completion evidence in `tasks.md`.
- Run focused tests after each logical change. Before declaring the feature complete, run the
  required pytest, ruff, and mypy checks from `CLAUDE.md` and complete the constitution audit.
- Do not introduce Docker, testcontainers, local PostgreSQL, free-form model-generated SQL, or
  direct Streamlit database access; these conflict with established project constraints.
