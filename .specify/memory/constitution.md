<!--
SYNC IMPACT REPORT
Version change: (unversioned template) → 1.0.0
Bump rationale: Initial ratification. No prior version existed; all placeholders replaced with
concrete governance for a regulated pharmaceutical commercial-data platform.

Principles defined (11; template scaffold supplied 5 slots, expanded per explicit user input):
  I.    Determinism First
  II.   No Autonomous Writes (NON-NEGOTIABLE)
  III.  Evidence-Bound Reasoning
  IV.   Simulate Before Propose
  V.    Full Auditability
  VI.   Least-Privilege Data Access
  VII.  PII/PHI Discipline
  VIII. Deterministic Orchestration
  IX.   Resumability
  X.    Testability
  XI.   The UI Is Not a Trust Boundary

Sections added:
  - Platform & Data Constraints  (was [SECTION_2_NAME])
  - Development Workflow & Quality Gates  (was [SECTION_3_NAME])
  - Governance

Sections removed: none.

Follow-up TODOs: none. RATIFICATION_DATE set to the date of first adoption (2026-08-24).

Downstream artifacts that read this file at runtime and now face 11 principles rather than the
scaffold's 5: .specify/templates/plan-template.md (Constitution Check gate),
.specify/templates/spec-template.md, .specify/templates/tasks-template.md. No edits made here —
per the command's scope guard, dependent templates read the constitution at runtime.
-->

# Agentic Data Quality Monitoring & Resolution Platform Constitution

This constitution governs an agentic data-quality platform operating on pharmaceutical commercial
data — sales transactions, HCP and HCO master data, product master, and territory alignment. The
domain is regulated and the output is used for commercial reporting. Every principle below exists
because violating it produces either an undetectable wrong answer or an unauditable change to
governed data.

These principles are structural requirements, not stylistic preferences. A design that satisfies a
principle only through prompt instruction to a model does not satisfy that principle.

## Core Principles

### I. Determinism First

Detection of data-quality failures MUST be deterministic, expressed as SQL rules executed in
PostgreSQL. Agents MUST NOT decide *whether* a record failed. Agents investigate, explain, assess
impact, and propose — nothing more.

Any agent output that contradicts a deterministic rule result is a defect, not a disagreement, and
MUST be treated as a bug against the agent rather than as evidence against the rule.

**Rationale:** A stochastic detector cannot be audited, reproduced, or defended to an inspector.
Separating detection (deterministic) from explanation (agentic) is what makes the agentic layer
safe to add at all: the baseline remains verifiable no matter how the agents behave.

### II. No Autonomous Writes (NON-NEGOTIABLE)

No agent may mutate curated or published commercial data under any circumstance. Agents MUST be
restricted to writing proposal, evidence, and audit tables.

Every correction to production data MUST pass through an explicit human-steward approval gate. The
gate MUST be enforced by the privilege model and the server-side API, not by agent instruction.

**Rationale:** This is the boundary that makes the entire system deployable in a regulated
environment. It is enforced structurally — through database roles (Principle VI) and server-side
re-validation (Principle XI) — precisely so that no prompt regression, model change, or injected
instruction can breach it.

### III. Evidence-Bound Reasoning

Every root-cause hypothesis, impact assessment, and remediation recommendation MUST cite persisted
evidence rows — query results, lineage records, historical comparisons — by ID.

Claims that cannot be traced to a persisted evidence record MUST be rejected before reaching a
steward. Rejection is a system behavior, not a reviewer responsibility.

**Rationale:** A confident, well-written, unciteable hypothesis is the most dangerous output this
system can produce, because it is indistinguishable from a correct one at review time. Requiring
citation by ID converts steward review from "does this sound right" into "does this evidence say
what the agent claims".

### IV. Simulate Before Propose

Every proposed correction MUST first be executed against an isolated sandbox schema, and the
simulation MUST report:

- rows affected
- rules that flip from fail to pass
- rules that regress from pass to fail
- referential-integrity effects

A proposal without an attached validation result MUST NOT be surfaced for approval.

**Rationale:** A correction that fixes the reported failure while silently breaking three other
rules is worse than no correction, and is invisible without simulation. Regression detection is the
substance of this principle; rows-affected alone is not sufficient.

### V. Full Auditability

Every agent step MUST be persisted to an append-only audit table with a correlation ID. Each record
MUST capture: input state, tool call, tool arguments, tool result reference, model identifier,
prompt version, token cost, latency, and the resulting decision.

For any published correction, the trail MUST be reconstructable end-to-end, from initial rule
failure through to the steward's approval, without reference to ephemeral logs.

**Rationale:** "Why did this number change?" must be answerable months later, by someone who was
not present, under inspection conditions. Append-only is required because a mutable audit trail
proves nothing.

### VI. Least-Privilege Data Access

Distinct database roles MUST exist for distinct capabilities, and no role may inherit another's
privileges:

- investigation agents connect under a read-only role
- remediation simulation connects under a sandbox-write role
- publishing connects under a separate role, reachable only after approval
- migrations connect under a dedicated migration role

The Supabase service-role key and any superuser credential MUST NOT be available to agent code, to
request handlers serving investigation reads, or to the user-interface process. They exist only in
the migration tooling's environment.

**Rationale:** Principle II states the rule; this principle is the mechanism that makes it true
even when everything above the database misbehaves. If an investigation agent physically cannot
issue a write, prompt injection into a data value cannot cause one.

### VII. PII/PHI Discipline

HCP identifiers and any patient-adjacent fields MUST be masked or tokenized before entering a model
prompt. Prompt payloads MUST be logged with masking already applied.

Masking MUST occur at the boundary where data is assembled into a prompt, not at the point of
logging — a payload that is masked only in logs was still transmitted unmasked.

**Rationale:** The audit trail required by Principle V multiplies exposure: every prompt is
persisted. Masking at the assembly boundary makes the audit trail safe to retain.

### VIII. Deterministic Orchestration

Control flow MUST live in explicit graph nodes and conditional edges, not in model free-choice.
Agent autonomy MUST be scoped to bounded ReAct loops inside individual nodes, each with a hard
iteration cap and a hard cost cap.

Every agentic node MUST terminate deterministically. On exhaustion of any bound, the node MUST
return a partial result rather than fail.

**Rationale:** Model-chosen control flow cannot be tested, cost-bounded, or reasoned about during
review. Bounded loops inside deterministic edges keep the system's behavior enumerable while
preserving genuine investigative autonomy where it earns its cost.

### IX. Resumability

Every workflow MUST be checkpointed. Human approval MUST be implemented as a durable interrupt that
can be resumed hours or days later without state loss.

Resumption MUST validate that the underlying data has not changed since the proposal was simulated.
A stale proposal MUST NOT be publishable on resume.

**Rationale:** Steward approval is measured in business days, not seconds. A workflow that cannot
survive that gap forces stewards into rushed decisions — and a workflow that resumes blindly
publishes a correction validated against data that no longer exists.

### X. Testability

Rules, tools, and graph nodes MUST be independently testable.

Agent behavior MUST be tested against a golden set of seeded, known-root-cause scenarios with
expected hypotheses and expected recommendations. The golden set MUST include at least one scenario
where the correct conclusion is "the data is correct, the alert is not actionable" and at least one
where the correct conclusion is "the rule is wrong, not the data".

**Rationale:** An agent that can only ever confirm a problem will erode steward trust faster than
one that is occasionally wrong. The negative scenarios are the ones that distinguish this system
from a dashboard, so they are constitutionally required rather than merely encouraged.

### XI. The UI Is Not a Trust Boundary

The user interface renders state and captures steward intent. It MUST NOT write to curated data,
MUST NOT hold a privileged credential, and MUST NOT enforce an approval rule on its own.

Every state-changing action MUST travel through a backend endpoint that independently re-validates
steward authority, proposal freshness, and constitution compliance server-side, on every call.

A control that is merely hidden in the UI is not a control.

**Rationale:** The interface layer is the easiest place to accidentally place a trust decision, and
the hardest place to defend one. Server-side re-validation means a forged, replayed, or
direct-to-API request is rejected identically to a malformed click.

## Platform & Data Constraints

**Single system of record.** Commercial data, data-quality metadata, workflow state, and the audit
trail MUST reside in one PostgreSQL database, so that agent state and business data share one
transaction boundary and one backup policy.

**Schema ownership.** All schema changes MUST be Alembic migrations held in version control. No
schema change may be made through a hosting-provider dashboard. Migrations MUST be reproducible
from a clean database.

**Connection discipline.** Migrations, the workflow checkpointer, and any code path relying on
prepared statements or advisory locks MUST use a session-scoped connection. Transaction-mode
connection pooling breaks both, and does so silently rather than with an error. Read-only query
paths that use a pooled connection MUST be documented as such.

**Schema separation.** Curated commercial data, data-quality metadata, workflow state, audit
records, and the remediation sandbox MUST occupy separate schemas, with role grants stated
explicitly per schema.

**Environment reality.** There is no local database and no container runtime available on the
development machine. Any design requiring `testcontainers`, `docker-compose`, or a local database
process is not implementable and MUST NOT be proposed. Integration tests run against the hosted
database using per-session schema isolation, with a session-start sweep that removes schemas
orphaned by crashed runs.

**Model configuration.** Model identifiers MUST be defined in a single configuration module and
never inlined at call sites. The reasoning tier MUST be replaceable by configuration change alone.

## Development Workflow & Quality Gates

**Specification precedes planning.** A specification describes WHAT and WHY. Naming a framework,
datastore, or library inside a specification converts it into a plan and MUST be corrected rather
than accepted.

**Clarification is mandatory.** The clarification step MUST NOT be skipped, regardless of tooling
defaults that mark it optional. In this domain, unresolved specification ambiguity does not surface
as a build error — it surfaces as plausible, confident, wrong agent behavior.

**Constitution check at plan time.** Every implementation plan MUST cite, for each principle, the
specific design element that enforces it, or explicitly flag it as unenforced. A plan that enforces
a principle solely by instructing a model does not satisfy that principle and MUST be revised.

**Golden scenarios precede agents.** Seeded golden-scenario data MUST exist before any agent
capability is implemented against it.

**Per-feature quality bars.** Every feature MUST ship with:

- contract tests for every agent tool
- integration tests against a seeded database
- a documented rollback path
- Pydantic-validated structured outputs at every agent boundary

**Cost and network awareness.** Tests that call a real model MUST be separately markable so the
default test command remains fast and free. Every integration test crosses the network to a hosted
database; suites MUST be designed with that latency budget in mind.

## Governance

**Supremacy.** This constitution supersedes all other development practices, conventions, and
tooling defaults. Where a tool's default conflicts with a principle here, the principle wins and
the deviation MUST be documented.

**Amendment procedure.** Amendments MUST be proposed as an explicit change to this document,
stating the principle affected, the rationale, and the migration impact on existing features. An
amendment that weakens Principle II, IV, VI, or XI MUST additionally state what compensating
control replaces the removed guarantee.

**Versioning policy.** This document follows semantic versioning:

- **MAJOR** — a principle is removed, or redefined in a way that invalidates existing compliant designs
- **MINOR** — a principle or governance section is added, or guidance is materially expanded
- **PATCH** — clarification, wording, or non-semantic refinement

**Compliance review.** Constitution compliance MUST be verified at two points in every feature:
during planning, and after implementation. The post-implementation check MUST demonstrate
compliance by evidence — a passing test, a role grant, a code path — not by assertion.

**Runtime guidance.** `CLAUDE.md` carries the operational conventions that implement these
principles day to day. It is subordinate to this document; where the two conflict, this document
governs and `CLAUDE.md` MUST be corrected.

**Version**: 1.0.0 | **Ratified**: 2026-08-24 | **Last Amended**: 2026-08-24
